from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from datetime import datetime, timedelta, date
from sqlalchemy import inspect, text
import random
import math
import os
import json
import logging
from pywebpush import webpush, WebPushException
from apscheduler.schedulers.background import BackgroundScheduler
from models import (
    db, User, Event, EventOption, Bet, Payout, SPORT_CHOICES, SponsorSpin, SpinPrize,
    VoucherRedemption, generate_voucher_code, PushSubscription,
)
from translations import t, get_locale, LANGUAGES, TRANSLATIONS

VOUCHER_EXPIRY_WARNING_DAYS = 2

REMEMBER_ME_DAYS = 30
REMEMBER_ME_COOKIE = "remembered_username"
SPIN_COOLDOWN_HOURS = 24
DEFAULT_SPIN_MIN = 5
DEFAULT_SPIN_MAX = 30
MIN_REGISTRATION_AGE = 18


def calculate_age(birth_date):
    today = date.today()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))

# 8 секторів колеса: частки діапазону [min_reward, max_reward] і вага ймовірності
# (переважання менших сум, останній сектор — рідкісний максимальний приз)
SPIN_SECTOR_FRACTIONS = [0.0, 0.05, 0.12, 0.22, 0.35, 0.5, 0.7, 1.0]
SPIN_SECTOR_WEIGHTS = [30, 22, 16, 12, 8, 6, 4, 2]


def _round_nice(value):
    if value >= 100:
        return round(value / 10) * 10
    if value >= 20:
        return round(value / 5) * 5
    return max(1, round(value))


def get_spin_sectors_from_range(sponsor):
    """Резервна логіка: рівномірний розподіл по діапазону min/max, якщо у спонсора не задано призів."""
    min_reward = sponsor.min_reward if sponsor else DEFAULT_SPIN_MIN
    max_reward = sponsor.max_reward if sponsor else DEFAULT_SPIN_MAX
    span = max_reward - min_reward
    return [_round_nice(min_reward + span * frac) for frac in SPIN_SECTOR_FRACTIONS]


def get_wheel_sectors(sponsor):
    """Повертає (sectors, weights), де sectors — список словників
    {"type": "coins"|"voucher", "label": str, "full_label": str, ...}, узгоджений
    з SpinPrize спонсора, або резервний список секторів-монет за діапазоном min/max.
    "label" — короткий текст/іконка НА секторі колеса; "full_label" — повний текст
    призу, показується лише на екрані результату після виграшу та в "Мої ваучери"."""
    if sponsor and sponsor.prizes:
        sectors = []
        weights = []
        for prize in sponsor.prizes:
            if prize.prize_type == "voucher":
                full_label = prize.custom_label or f"🎁 -{prize.voucher_percent}%"
                sectors.append({
                    "type": "voucher",
                    "label": prize.custom_icon or "🎁",
                    "full_label": full_label,
                    "percent": prize.voucher_percent,
                    "prize_id": prize.id,
                })
            else:
                amount = prize.coins_amount
                amount_str = str(int(amount)) if amount == int(amount) else str(amount)
                label = f"🪙 {amount_str}"
                sectors.append({"type": "coins", "label": label, "full_label": label, "amount": amount, "prize_id": prize.id})
            weights.append(max(1, prize.weight))
        return sectors, weights

    amounts = get_spin_sectors_from_range(sponsor)
    sectors = [{"type": "coins", "label": f"🪙 {v}", "full_label": f"🪙 {v}", "amount": v} for v in amounts]
    return sectors, SPIN_SECTOR_WEIGHTS


def _sector_point(angle_deg):
    """Точка на колі (0% 0% — верхній лівий кут квадрата 0..100%), де 0deg — верх, за годинниковою."""
    rad = math.radians(angle_deg)
    x = 50 + 50 * math.sin(rad)
    y = 50 - 50 * math.cos(rad)
    return f"{x:.2f}% {y:.2f}%"


def build_sector_clip_path(start_deg, end_deg, steps=10):
    """clip-path polygon для сектора кола (центр + точки уздовж дуги), щоб контур
    сектора точно повторював дугу, а не пряму хорду."""
    points = ["50% 50%"]
    span = end_deg - start_deg
    for i in range(steps + 1):
        points.append(_sector_point(start_deg + span * i / steps))
    return "polygon(" + ", ".join(points) + ")"


def attach_sector_geometry(sectors):
    n = len(sectors)
    step = 360 / n
    for i, sector in enumerate(sectors):
        sector["clip_path"] = build_sector_clip_path(i * step, (i + 1) * step)
    return sectors


def generate_unique_voucher_code():
    for _ in range(10):
        code = generate_voucher_code()
        if not VoucherRedemption.query.filter_by(unique_code=code).first():
            return code
    raise RuntimeError("Не вдалося згенерувати унікальний код ваучера")


app = Flask(__name__)

# SECRET_KEY підписує сесії/куки (зокрема remember-me). В проді ОБОВ'ЯЗКОВО
# задавати через змінну середовища — інакше будь-хто, хто має цей рядок
# у коді (напр. в git-репозиторії), зможе підробляти сесії користувачів.
# Дефолт нижче — лише заглушка для локальної розробки.
_SECRET_KEY = os.environ.get("SECRET_KEY")
if not _SECRET_KEY:
    _SECRET_KEY = "dev-insecure-secret-key-do-not-use-in-production"
    logging.warning(
        "Змінна середовища SECRET_KEY не задана — використовується небезпечний "
        "дефолт лише для локальної розробки. У продакшені обов'язково задайте SECRET_KEY."
    )
app.config["SECRET_KEY"] = _SECRET_KEY

# На Render (та деяких інших хостах) DATABASE_URL віддається як "postgres://"
# або "postgresql://", а нам потрібен диалект psycopg3 — "postgresql+psycopg://".
# Без явного диалекту SQLAlchemy шукатиме psycopg2 за замовчуванням, якого
# в проєкті більше нема (замінили на psycopg[binary]) — create_engine впаде.
_DATABASE_URL = os.environ.get("DATABASE_URL")
if _DATABASE_URL and _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif _DATABASE_URL and _DATABASE_URL.startswith("postgresql://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = _DATABASE_URL or "sqlite:///betfree.db"
db.init_app(app)

# ---------- МУЛЬТИМОВНІСТЬ (EN / UK / DE) ----------


@app.context_processor
def inject_i18n():
    return dict(t=t, current_lang=get_locale(), LANGUAGES=LANGUAGES)


@app.route("/set-language/<lang_code>")
def set_language(lang_code):
    if lang_code in TRANSLATIONS:
        session["lang"] = lang_code
    return redirect(request.referrer or url_for("dashboard"))

# ---------- WEB PUSH: VAPID-ключі ----------
# Ключі генеруються скриптом generate_vapid_keys.py в instance/ (поза git).
# Якщо ключів ще нема — push просто вимкнений, решта застосунку працює як завжди.
VAPID_PRIVATE_KEY_PATH = os.environ.get(
    "VAPID_PRIVATE_KEY_PATH", os.path.join(app.instance_path, "vapid_private.pem")
)
VAPID_PUBLIC_KEY_PATH = os.path.join(app.instance_path, "vapid_public_key.txt")
VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", "mailto:admin@betfree.local")

VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY")
if not VAPID_PUBLIC_KEY and os.path.exists(VAPID_PUBLIC_KEY_PATH):
    with open(VAPID_PUBLIC_KEY_PATH, "r") as f:
        VAPID_PUBLIC_KEY = f.read().strip()

PUSH_ENABLED = bool(VAPID_PUBLIC_KEY and os.path.exists(VAPID_PRIVATE_KEY_PATH))
if not PUSH_ENABLED:
    logging.warning(
        "VAPID-ключі не знайдено — push-сповіщення вимкнені. "
        "Запустіть `python generate_vapid_keys.py`, щоб їх згенерувати."
    )


def send_push_notification(user_id, title, body):
    """Надсилає push усім активним підпискам користувача. Мовчки виходить,
    якщо push не налаштовано (немає VAPID-ключів) — це не критична функція."""
    if not PUSH_ENABLED:
        return

    subscriptions = PushSubscription.query.filter_by(user_id=user_id).all()
    payload = json.dumps({"title": title, "body": body, "icon": "/static/icons/icon-192.png"})

    for sub in subscriptions:
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh_key, "auth": sub.auth_key},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY_PATH,
                vapid_claims={"sub": VAPID_CLAIMS_EMAIL},
            )
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410):
                # підписка більше не існує (юзер відписався/скинув дозвіл) — прибираємо
                db.session.delete(sub)
                db.session.commit()
            else:
                logging.warning("Не вдалося надіслати push користувачу %s: %s", user_id, exc)


@app.route("/sw.js")
def service_worker():
    return app.send_static_file("sw.js")


@app.route("/push/vapid-public-key")
@login_required
def push_vapid_public_key():
    return jsonify(publicKey=VAPID_PUBLIC_KEY, enabled=PUSH_ENABLED)


@app.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    keys = data.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")

    if not endpoint or not p256dh or not auth:
        return jsonify(ok=False, message=t("push.invalid_subscription")), 400

    existing = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if existing:
        existing.user_id = current_user.id
        existing.p256dh_key = p256dh
        existing.auth_key = auth
    else:
        db.session.add(PushSubscription(
            user_id=current_user.id,
            endpoint=endpoint,
            p256dh_key=p256dh,
            auth_key=auth,
        ))
    db.session.commit()
    return jsonify(ok=True)


def check_and_send_notifications():
    """Періодична фонова перевірка (APScheduler): надсилає push, коли
    1) ваучер юзера спливає протягом 24 годин, 2) кулдаун рулетки закінчився.
    Прапорці *_notified / *_reminder_sent захищають від повторної відправки."""
    if not PUSH_ENABLED:
        return

    with app.app_context():
        now = datetime.utcnow()

        expiring_vouchers = VoucherRedemption.query.filter(
            VoucherRedemption.is_used.is_(False),
            VoucherRedemption.expiry_reminder_sent.is_(False),
            VoucherRedemption.expires_at > now,
            VoucherRedemption.expires_at <= now + timedelta(hours=24),
        ).all()
        for voucher in expiring_vouchers:
            percent = voucher.prize.voucher_percent if voucher.prize else "?"
            send_push_notification(
                voucher.user_id,
                t("push.voucher_expiring_title"),
                t(
                    "push.voucher_expiring_body",
                    sponsor=voucher.sponsor.sponsor_name,
                    percent=percent,
                    date=voucher.expires_at.strftime('%d.%m.%Y %H:%M'),
                ),
            )
            voucher.expiry_reminder_sent = True
        if expiring_vouchers:
            db.session.commit()

        ready_users = User.query.filter(
            User.last_spin_at.isnot(None),
            User.spin_ready_notified.is_(False),
        ).all()
        notified_any = False
        for user in ready_users:
            if user.last_spin_at + timedelta(hours=SPIN_COOLDOWN_HOURS) <= now:
                send_push_notification(
                    user.id, t("push.roulette_ready_title"), t("push.roulette_ready_body")
                )
                user.spin_ready_notified = True
                notified_any = True
        if notified_any:
            db.session.commit()


def start_notification_scheduler():
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(check_and_send_notifications, "interval", minutes=5, id="push_notifications")
    scheduler.start()
    return scheduler


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
User.is_authenticated = property(lambda self: True)
User.is_active = property(lambda self: True)
User.is_anonymous = property(lambda self: False)
User.get_id = lambda self: str(self.id)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------- AUTH ----------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        email = request.form["email"]
        password = request.form["password"]
        date_of_birth_raw = request.form.get("date_of_birth", "").strip()
        age_confirmed = bool(request.form.get("age_confirm"))

        if User.query.filter_by(username=username).first():
            flash(t("auth.username_taken"))
            return redirect(url_for("register"))

        if User.query.filter_by(email=email).first():
            flash(t("auth.email_taken"))
            return redirect(url_for("register"))

        try:
            date_of_birth = datetime.strptime(date_of_birth_raw, "%Y-%m-%d").date()
        except ValueError:
            flash(t("auth.invalid_birthdate"))
            return redirect(url_for("register"))

        if date_of_birth > date.today():
            flash(t("auth.invalid_birthdate"))
            return redirect(url_for("register"))

        if not age_confirmed or calculate_age(date_of_birth) < MIN_REGISTRATION_AGE:
            flash(t("auth.age_restriction"))
            return redirect(url_for("register"))

        user = User(
            username=username,
            email=email,
            balance=100.0,  # стартовий тестовий баланс
            date_of_birth=date_of_birth,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash(t("auth.register_success"))
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        remember = bool(request.form.get("remember_me"))
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user, remember=remember, duration=timedelta(days=REMEMBER_ME_DAYS))
            response = redirect(url_for("dashboard"))
            if remember:
                response.set_cookie(
                    REMEMBER_ME_COOKIE,
                    username,
                    max_age=REMEMBER_ME_DAYS * 24 * 60 * 60,
                )
            else:
                response.delete_cookie(REMEMBER_ME_COOKIE)
            return response
        flash(t("auth.invalid_credentials"))

    remembered_username = request.cookies.get(REMEMBER_ME_COOKIE, "")
    return render_template("login.html", remembered_username=remembered_username)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------- ПРОФІЛЬ ----------

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_user.email = request.form["email"]
        current_user.country = request.form.get("country", "").strip() or None
        current_user.phone = request.form.get("phone", "").strip() or None
        db.session.commit()
        flash(t("profile.updated"))
        return redirect(url_for("profile"))

    active_voucher_count = VoucherRedemption.query.filter(
        VoucherRedemption.user_id == current_user.id,
        VoucherRedemption.is_used.is_(False),
        VoucherRedemption.expires_at > datetime.utcnow(),
    ).count()

    return render_template("profile.html", active_voucher_count=active_voucher_count)


@app.route("/profile/withdraw", methods=["POST"])
@login_required
def profile_withdraw():
    flash(t("profile.withdraw_soon"))
    return redirect(url_for("profile"))


@app.route("/my-vouchers")
@login_required
def my_vouchers():
    vouchers = (
        VoucherRedemption.query.filter_by(user_id=current_user.id)
        .order_by(VoucherRedemption.created_at.desc())
        .all()
    )
    return render_template("my_vouchers.html", vouchers=vouchers)


# ---------- АДМІН: КОРИСТУВАЧІ ----------

@app.route("/admin/users")
@login_required
def admin_users():
    if not current_user.is_admin:
        flash(t("admin.only_admin_view"))
        return redirect(url_for("dashboard"))

    users = User.query.order_by(User.username).all()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/toggle-verify", methods=["POST"])
@login_required
def toggle_verify(user_id):
    if not current_user.is_admin:
        flash(t("admin.only_admin_action"))
        return redirect(url_for("dashboard"))

    user = User.query.get_or_404(user_id)
    user.is_verified = not user.is_verified
    db.session.commit()
    flash(t("admin.verify_toggled", username=user.username))
    return redirect(url_for("admin_users"))


# ---------- РУЛЕТКА ----------

@app.route("/roulette", methods=["GET", "POST"])
@login_required
def roulette():
    sponsor = SponsorSpin.query.filter_by(is_active=True).order_by(SponsorSpin.id).first()

    next_available_at = None
    if current_user.last_spin_at:
        next_available_at = current_user.last_spin_at + timedelta(hours=SPIN_COOLDOWN_HOURS)
    can_spin = next_available_at is None or datetime.utcnow() >= next_available_at

    if request.method == "POST":
        if not can_spin:
            return jsonify(ok=False, message=t("roulette.not_available")), 400

        # той самий (перемішаний) порядок секторів, що бачив користувач на сторінці,
        # інакше sector_index не відповідатиме тому, що намальовано на колесі
        sectors = session.get("wheel_sectors")
        weights = session.get("wheel_weights")
        if not sectors or not weights or len(sectors) != len(weights):
            sectors, weights = get_wheel_sectors(sponsor)

        sector_index = random.choices(range(len(sectors)), weights=weights, k=1)[0]
        chosen = sectors[sector_index]

        if chosen["type"] == "voucher":
            validity_days = sponsor.voucher_validity_days if sponsor else 7
            voucher = VoucherRedemption(
                user_id=current_user.id,
                sponsor_spin_id=sponsor.id,
                spin_prize_id=chosen.get("prize_id"),
                unique_code=generate_unique_voucher_code(),
                expires_at=datetime.utcnow() + timedelta(days=validity_days),
            )
            db.session.add(voucher)

            result = dict(
                prize_type="voucher",
                percent=chosen["percent"],
                label=chosen["full_label"],
                promo_description=sponsor.promo_description if sponsor else None,
                voucher_code=voucher.unique_code,
                voucher_expires=voucher.expires_at.strftime("%d.%m.%Y"),
            )
        else:
            reward = chosen["amount"]
            current_user.balance += reward
            result = dict(prize_type="coins", amount=reward)

        current_user.last_spin_at = datetime.utcnow()
        current_user.spin_ready_notified = False  # скидаємо, щоб наступне закінчення кулдауну знову сповістило
        db.session.commit()

        next_at = current_user.last_spin_at + timedelta(hours=SPIN_COOLDOWN_HOURS)

        return jsonify(
            ok=True,
            sector_index=sector_index,
            balance=current_user.balance,
            next_available_at=next_at.isoformat() + "Z",
            **result,
        )

    sectors, weights = get_wheel_sectors(sponsor)
    order = list(range(len(sectors)))
    random.shuffle(order)
    sectors = [sectors[i] for i in order]
    weights = [weights[i] for i in order]
    attach_sector_geometry(sectors)

    session["wheel_sectors"] = sectors
    session["wheel_weights"] = weights

    return render_template(
        "roulette.html",
        sponsor=sponsor,
        sectors=sectors,
        can_spin=can_spin,
        next_available_at=next_available_at,
    )


# ---------- АДМІН: СПОНСОРИ (CRUD) ----------

def _optional_form_fields(*names):
    """Витягує з request.form список необов'язкових текстових полів
    (пусте значення -> None), щоб не дублювати .strip() or None у create/edit."""
    return {name: request.form.get(name, "").strip() or None for name in names}


SPONSOR_OPTIONAL_FIELDS = (
    "sponsor_logo_url", "website_url", "brand_color_primary", "brand_color_secondary",
    "slogan", "promo_code", "promo_description",
    "contact_address", "contact_email", "contact_phone", "contact_person_name",
)


@app.route("/admin/sponsors")
@login_required
def admin_sponsors():
    if not current_user.is_admin:
        flash(t("admin.only_admin_view"))
        return redirect(url_for("dashboard"))

    sponsors = SponsorSpin.query.order_by(SponsorSpin.created_at.desc()).all()
    event_counts = dict(
        db.session.query(Event.sponsor_spin_id, db.func.count(Event.id))
        .filter(Event.sponsor_spin_id.isnot(None))
        .group_by(Event.sponsor_spin_id)
        .all()
    )
    return render_template("admin_sponsors.html", sponsors=sponsors, event_counts=event_counts)


@app.route("/admin/sponsors/new")
@login_required
def new_sponsor():
    if not current_user.is_admin:
        flash(t("admin.only_admin_view"))
        return redirect(url_for("dashboard"))
    return render_template("admin_sponsor_detail.html", sponsor=None, event_count=0)


@app.route("/admin/sponsors/<int:sponsor_id>")
@login_required
def sponsor_detail(sponsor_id):
    if not current_user.is_admin:
        flash(t("admin.only_admin_view"))
        return redirect(url_for("dashboard"))

    sponsor = SponsorSpin.query.get_or_404(sponsor_id)
    event_count = Event.query.filter_by(sponsor_spin_id=sponsor.id).count()
    return render_template("admin_sponsor_detail.html", sponsor=sponsor, event_count=event_count)


@app.route("/admin/sponsors", methods=["POST"])
@login_required
def create_sponsor():
    if not current_user.is_admin:
        flash(t("admin.only_admin_action"))
        return redirect(url_for("dashboard"))

    sponsor_name = request.form["sponsor_name"].strip()
    min_reward = float(request.form["min_reward"])
    max_reward = float(request.form["max_reward"])
    voucher_validity_days = int(request.form.get("voucher_validity_days") or 7)
    extra = _optional_form_fields(*SPONSOR_OPTIONAL_FIELDS)

    if min_reward > max_reward:
        flash(t("admin.min_max_error"))
        return redirect(url_for("new_sponsor"))

    sponsor = SponsorSpin(
        sponsor_name=sponsor_name,
        min_reward=min_reward,
        max_reward=max_reward,
        voucher_validity_days=voucher_validity_days,
        **extra,
    )
    db.session.add(sponsor)
    db.session.commit()
    flash(t("admin.sponsor_added"))
    return redirect(url_for("sponsor_detail", sponsor_id=sponsor.id))


@app.route("/admin/sponsors/<int:sponsor_id>/edit", methods=["POST"])
@login_required
def edit_sponsor(sponsor_id):
    if not current_user.is_admin:
        flash(t("admin.only_admin_action"))
        return redirect(url_for("dashboard"))

    sponsor = SponsorSpin.query.get_or_404(sponsor_id)
    min_reward = float(request.form["min_reward"])
    max_reward = float(request.form["max_reward"])

    if min_reward > max_reward:
        flash(t("admin.min_max_error"))
        return redirect(url_for("sponsor_detail", sponsor_id=sponsor_id))

    sponsor.sponsor_name = request.form["sponsor_name"].strip()
    sponsor.min_reward = min_reward
    sponsor.max_reward = max_reward
    sponsor.voucher_validity_days = int(request.form.get("voucher_validity_days") or 7)
    for field, value in _optional_form_fields(*SPONSOR_OPTIONAL_FIELDS).items():
        setattr(sponsor, field, value)
    db.session.commit()
    flash(t("admin.sponsor_updated", name=sponsor.sponsor_name))
    return redirect(url_for("sponsor_detail", sponsor_id=sponsor_id))


@app.route("/admin/sponsors/<int:sponsor_id>/toggle", methods=["POST"])
@login_required
def toggle_sponsor(sponsor_id):
    if not current_user.is_admin:
        flash(t("admin.only_admin_action"))
        return redirect(url_for("dashboard"))

    sponsor = SponsorSpin.query.get_or_404(sponsor_id)
    sponsor.is_active = not sponsor.is_active
    db.session.commit()
    status_word = t("common.activated") if sponsor.is_active else t("common.deactivated")
    flash(t("admin.sponsor_toggled", name=sponsor.sponsor_name, status=status_word))
    return redirect(url_for("sponsor_detail", sponsor_id=sponsor_id))


@app.route("/admin/sponsors/<int:sponsor_id>/delete", methods=["POST"])
@login_required
def delete_sponsor(sponsor_id):
    if not current_user.is_admin:
        flash(t("admin.only_admin_action"))
        return redirect(url_for("dashboard"))

    sponsor = SponsorSpin.query.get_or_404(sponsor_id)

    # ваучери зберігають історію видач — не дозволяємо знищити її разом зі спонсором;
    # пропонуємо натомість деактивувати (soft, через toggle_sponsor)
    if VoucherRedemption.query.filter_by(sponsor_spin_id=sponsor.id).first():
        flash(t("admin.sponsor_delete_blocked", name=sponsor.sponsor_name))
        return redirect(url_for("sponsor_detail", sponsor_id=sponsor_id))

    # події лишаються, просто втрачають бренд спонсора (sponsor_spin_id nullable)
    Event.query.filter_by(sponsor_spin_id=sponsor.id).update({"sponsor_spin_id": None})

    sponsor_name = sponsor.sponsor_name
    db.session.delete(sponsor)  # каскадно видалить його SpinPrize (cascade="all, delete-orphan")
    db.session.commit()
    flash(t("admin.sponsor_deleted", name=sponsor_name))
    return redirect(url_for("admin_sponsors"))


# ---------- АДМІН: РУЛЕТКА (призи колеса за спонсором) ----------

@app.route("/admin/roulette")
@login_required
def admin_roulette():
    if not current_user.is_admin:
        flash(t("admin.only_admin_view"))
        return redirect(url_for("dashboard"))

    sponsors = SponsorSpin.query.order_by(SponsorSpin.created_at.desc()).all()
    return render_template("admin_roulette.html", sponsors=sponsors)


@app.route("/admin/roulette/<int:sponsor_id>/prizes", methods=["POST"])
@login_required
def add_prize(sponsor_id):
    if not current_user.is_admin:
        flash(t("admin.only_admin_action"))
        return redirect(url_for("dashboard"))

    sponsor = SponsorSpin.query.get_or_404(sponsor_id)
    prize_type = request.form["prize_type"]
    weight = int(request.form["weight"])

    prize = SpinPrize(sponsor_spin_id=sponsor.id, prize_type=prize_type, weight=weight)
    if prize_type == "voucher":
        prize.voucher_percent = int(request.form["voucher_percent"])
        prize.custom_label = request.form.get("custom_label", "").strip() or None
        prize.custom_icon = request.form.get("custom_icon", "").strip() or None
    else:
        prize.coins_amount = float(request.form["coins_amount"])

    db.session.add(prize)
    db.session.commit()
    flash(t("admin.prize_added"))
    return redirect(url_for("admin_roulette"))


@app.route("/admin/roulette/prizes/<int:prize_id>/delete", methods=["POST"])
@login_required
def delete_prize(prize_id):
    if not current_user.is_admin:
        flash(t("admin.only_admin_action"))
        return redirect(url_for("dashboard"))

    prize = SpinPrize.query.get_or_404(prize_id)
    db.session.delete(prize)
    db.session.commit()
    flash(t("admin.prize_deleted"))
    return redirect(url_for("admin_roulette"))


# ---------- АДМІН: ПЕРЕВІРКА ВАУЧЕРІВ ----------

@app.route("/admin/verify-voucher", methods=["GET", "POST"])
@login_required
def admin_verify_voucher():
    if not current_user.is_admin:
        flash(t("admin.only_admin_view"))
        return redirect(url_for("dashboard"))

    voucher = None
    searched_code = ""

    if request.method == "POST":
        action = request.form.get("action", "search")

        if action == "confirm":
            voucher_id = int(request.form["voucher_id"])
            voucher = VoucherRedemption.query.get_or_404(voucher_id)
            searched_code = voucher.unique_code

            if voucher.is_expired:
                flash(t("admin.voucher_expired_flash"))
            else:
                # атомарний UPDATE замість read-then-write: якщо форму
                # надіслали двічі поспіль (або два адміни одночасно),
                # тільки один запит реально позначить ваучер використаним
                updated_rows = VoucherRedemption.query.filter_by(
                    id=voucher_id, is_used=False
                ).update({"is_used": True, "used_at": datetime.utcnow()})
                db.session.commit()

                if updated_rows:
                    flash(t("admin.voucher_confirmed", code=searched_code))
                else:
                    flash(t("admin.voucher_already_used"))
                voucher = VoucherRedemption.query.get(voucher_id)  # свіжий стан для картки нижче
        else:
            searched_code = request.form.get("unique_code", "").strip().upper()
            voucher = VoucherRedemption.query.filter_by(unique_code=searched_code).first()
            if not voucher:
                flash(t("admin.voucher_not_found"))

    return render_template("admin_verify_voucher.html", voucher=voucher, searched_code=searched_code)


# ---------- DASHBOARD ----------

@app.route("/")
@login_required
def dashboard():
    events = Event.query.filter(Event.status != "settled").order_by(Event.match_date).all()

    expiring_voucher = (
        VoucherRedemption.query.filter(
            VoucherRedemption.user_id == current_user.id,
            VoucherRedemption.is_used.is_(False),
            VoucherRedemption.expires_at > datetime.utcnow(),
            VoucherRedemption.expires_at <= datetime.utcnow() + timedelta(days=VOUCHER_EXPIRY_WARNING_DAYS),
        )
        .order_by(VoucherRedemption.expires_at)
        .first()
    )

    return render_template("dashboard.html", events=events, expiring_voucher=expiring_voucher)


# ---------- EVENTS (ADMIN / РЕКЛАМОДАВЕЦЬ) ----------

def _parse_prize_type_fields(form, sponsor_spin_id):
    """Читає prize_type/voucher_count з форми події й валідує їх:
    для voucher/mixed потрібен і обраний спонсор (звідти береться текст
    ваучера — promo_code/promo_description), і кількість ваучерів > 0.
    Повертає (prize_type, voucher_count, error_message_or_None)."""
    prize_type = form.get("prize_type", "money")
    if prize_type not in ("money", "voucher", "mixed"):
        prize_type = "money"

    if prize_type == "money":
        return prize_type, None, None

    if not sponsor_spin_id:
        return prize_type, None, t("events.sponsor_required_for_voucher")

    voucher_count_raw = form.get("voucher_count", "").strip()
    if not voucher_count_raw or int(voucher_count_raw) <= 0:
        return prize_type, None, t("events.voucher_count_required")

    return prize_type, int(voucher_count_raw), None


@app.route("/event/create", methods=["GET", "POST"])
@login_required
def create_event():
    if not current_user.is_admin:
        flash(t("events.only_admin_create"))
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        title = request.form["title"]
        description = request.form["description"]
        sport_type = request.form["sport_type"]
        # поле приховане й необов'язкове на клієнті, коли тип призу "voucher"
        prize_pool = float(request.form.get("prize_pool") or 0)
        match_date = datetime.strptime(request.form["match_date"], "%Y-%m-%dT%H:%M")
        options = request.form.getlist("options")
        sponsor_spin_id = request.form.get("sponsor_spin_id") or None

        prize_type, voucher_count, error = _parse_prize_type_fields(request.form, sponsor_spin_id)
        if error:
            flash(error)
            return redirect(url_for("create_event"))

        event = Event(
            title=title,
            description=description,
            sport_type=sport_type,
            prize_pool=prize_pool,
            prize_type=prize_type,
            voucher_count=voucher_count,
            match_date=match_date,
            created_by=current_user.id,
            sponsor_spin_id=int(sponsor_spin_id) if sponsor_spin_id else None,
        )
        db.session.add(event)
        db.session.flush()  # щоб отримати event.id до коміту

        for opt_text in options:
            if opt_text.strip():
                db.session.add(EventOption(event_id=event.id, option_text=opt_text.strip()))

        db.session.commit()
        flash(t("events.created"))
        return redirect(url_for("dashboard"))

    sponsors = SponsorSpin.query.filter_by(is_active=True).order_by(SponsorSpin.sponsor_name).all()
    return render_template("create_event.html", sport_choices=SPORT_CHOICES, sponsors=sponsors)


@app.route("/event/<int:event_id>/edit", methods=["GET", "POST"])
@login_required
def edit_event(event_id):
    if not current_user.is_admin:
        flash(t("events.only_admin_edit"))
        return redirect(url_for("dashboard"))

    event = Event.query.get_or_404(event_id)

    if request.method == "POST":
        sponsor_spin_id = request.form.get("sponsor_spin_id") or None
        prize_type, voucher_count, error = _parse_prize_type_fields(request.form, sponsor_spin_id)
        if error:
            flash(error)
            return redirect(url_for("edit_event", event_id=event.id))

        event.title = request.form["title"]
        event.description = request.form["description"]
        event.sport_type = request.form["sport_type"]
        # поле приховане й необов'язкове на клієнті, коли тип призу "voucher"
        event.prize_pool = float(request.form.get("prize_pool") or 0)
        event.prize_type = prize_type
        event.voucher_count = voucher_count
        event.match_date = datetime.strptime(request.form["match_date"], "%Y-%m-%dT%H:%M")
        event.sponsor_spin_id = int(sponsor_spin_id) if sponsor_spin_id else None

        db.session.commit()
        flash(t("events.updated"))
        return redirect(url_for("view_event", event_id=event.id))

    sponsors = SponsorSpin.query.filter_by(is_active=True).order_by(SponsorSpin.sponsor_name).all()
    return render_template("edit_event.html", event=event, sport_choices=SPORT_CHOICES, sponsors=sponsors)


@app.route("/event/<int:event_id>")
@login_required
def view_event(event_id):
    event = Event.query.get_or_404(event_id)
    user_bet = Bet.query.filter_by(user_id=current_user.id, event_id=event_id).first()
    return render_template("event.html", event=event, user_bet=user_bet)


# ---------- BETTING ----------

@app.route("/event/<int:event_id>/bet", methods=["POST"])
@login_required
def place_bet(event_id):
    event = Event.query.get_or_404(event_id)

    if event.status != "open":
        flash(t("events.closed"))
        return redirect(url_for("view_event", event_id=event_id))

    option_id = int(request.form["option_id"])

    existing = Bet.query.filter_by(user_id=current_user.id, event_id=event_id).first()
    if existing:
        flash(t("events.already_bet"))
        return redirect(url_for("view_event", event_id=event_id))

    bet = Bet(user_id=current_user.id, event_id=event_id, option_id=option_id)
    db.session.add(bet)
    db.session.commit()

    flash(t("events.bet_accepted"))
    return redirect(url_for("view_event", event_id=event_id))


# ---------- SETTLEMENT (ADMIN) ----------

@app.route("/event/<int:event_id>/settle", methods=["POST"])
@login_required
def settle_event(event_id):
    if not current_user.is_admin:
        flash(t("events.only_admin_settle"))
        return redirect(url_for("dashboard"))

    event = Event.query.get_or_404(event_id)
    winning_option_id = int(request.form["winning_option_id"])

    for opt in event.options:
        opt.is_winner = (opt.id == winning_option_id)

    winning_bets = Bet.query.filter_by(event_id=event_id, option_id=winning_option_id).all()

    # pool-модель: приз ділиться порівну між усіма, хто вгадав переможця;
    # якщо ніхто не вгадав — призовий фонд нікому не роздається
    if winning_bets:
        if event.prize_type in ("money", "mixed"):
            payout_amount = round(event.prize_pool / len(winning_bets), 2)
            for bet in winning_bets:
                user = User.query.get(bet.user_id)
                user.balance += payout_amount
                db.session.add(Payout(user_id=user.id, event_id=event_id, amount=payout_amount))

        if event.prize_type in ("voucher", "mixed") and event.voucher_count:
            # лотерея серед переможців: скільки ваучерів — стільки випадкових
            # переможців їх отримають, решта в цьому режимі не отримує нічого
            lucky_bets = random.sample(winning_bets, min(event.voucher_count, len(winning_bets)))
            validity_days = event.sponsor.voucher_validity_days if event.sponsor else 7
            for bet in lucky_bets:
                db.session.add(VoucherRedemption(
                    user_id=bet.user_id,
                    sponsor_spin_id=event.sponsor_spin_id,
                    event_id=event.id,
                    unique_code=generate_unique_voucher_code(),
                    expires_at=datetime.utcnow() + timedelta(days=validity_days),
                ))

    event.status = "settled"
    db.session.commit()

    flash(t("events.settled"))
    return redirect(url_for("dashboard"))


def migrate_user_columns():
    """Легка авто-міграція SQLite: додає нові nullable-колонки User,
    якщо вони відсутні в уже існуючій таблиці (create_all їх не додає)."""
    inspector = inspect(db.engine)
    if "user" not in inspector.get_table_names():
        return

    existing_cols = {col["name"] for col in inspector.get_columns("user")}
    new_columns = {
        "country": "VARCHAR(80)",
        "phone": "VARCHAR(30)",
        "is_verified": "BOOLEAN DEFAULT 0",
        "last_spin_at": "DATETIME",
        "spin_ready_notified": "BOOLEAN DEFAULT 0",
        "date_of_birth": "DATE",
    }
    with db.engine.connect() as conn:
        for col_name, col_type in new_columns.items():
            if col_name not in existing_cols:
                conn.execute(text(f'ALTER TABLE "user" ADD COLUMN {col_name} {col_type}'))
        conn.commit()


def migrate_sponsor_spin_columns():
    """Легка авто-міграція SQLite: додає нові nullable-колонки SponsorSpin,
    якщо вони відсутні в уже існуючій таблиці (create_all їх не додає)."""
    inspector = inspect(db.engine)
    if "sponsor_spin" not in inspector.get_table_names():
        return

    existing_cols = {col["name"] for col in inspector.get_columns("sponsor_spin")}
    new_columns = {
        "promo_code": "VARCHAR(50)",
        "promo_description": "VARCHAR(255)",
        "voucher_validity_days": "INTEGER DEFAULT 7",
        "website_url": "VARCHAR(500)",
        "brand_color_primary": "VARCHAR(7)",
        "brand_color_secondary": "VARCHAR(7)",
        "slogan": "VARCHAR(255)",
        "contact_address": "VARCHAR(255)",
        "contact_email": "VARCHAR(120)",
        "contact_phone": "VARCHAR(30)",
        "contact_person_name": "VARCHAR(120)",
    }
    with db.engine.connect() as conn:
        for col_name, col_type in new_columns.items():
            if col_name not in existing_cols:
                conn.execute(text(f"ALTER TABLE sponsor_spin ADD COLUMN {col_name} {col_type}"))
        conn.commit()


def migrate_spin_prize_columns():
    """Легка авто-міграція SQLite: додає нові nullable-колонки SpinPrize,
    якщо вони відсутні в уже існуючій таблиці (create_all їх не додає)."""
    inspector = inspect(db.engine)
    if "spin_prize" not in inspector.get_table_names():
        return

    existing_cols = {col["name"] for col in inspector.get_columns("spin_prize")}
    new_columns = {
        "sponsor_logo_url": "VARCHAR(500)",
        "custom_label": "VARCHAR(100)",
        "custom_icon": "VARCHAR(10)",
    }
    with db.engine.connect() as conn:
        for col_name, col_type in new_columns.items():
            if col_name not in existing_cols:
                conn.execute(text(f"ALTER TABLE spin_prize ADD COLUMN {col_name} {col_type}"))
        conn.commit()


def migrate_voucher_redemption_columns():
    """Легка авто-міграція SQLite: додає нові nullable-колонки VoucherRedemption,
    якщо вони відсутні в уже існуючій таблиці (create_all їх не додає)."""
    inspector = inspect(db.engine)
    if "voucher_redemption" not in inspector.get_table_names():
        return

    existing_cols = {col["name"] for col in inspector.get_columns("voucher_redemption")}
    new_columns = {
        "expiry_reminder_sent": "BOOLEAN DEFAULT 0",
        "event_id": "INTEGER",  # ваучер від події (окремо від sponsor_spin_id/spin_prize_id рулетки)
    }
    with db.engine.connect() as conn:
        for col_name, col_type in new_columns.items():
            if col_name not in existing_cols:
                conn.execute(text(f"ALTER TABLE voucher_redemption ADD COLUMN {col_name} {col_type}"))
        conn.commit()

    # sponsor_spin_id раніше був NOT NULL (ваучер завжди від спонсора рулетки);
    # тепер ваучер може прийти й від події (event_id), без власного спонсора.
    # SQLite не вміє ALTER COLUMN DROP NOT NULL — пропускаємо (локальна БД
    # одноразова, поза git); Postgres (прод) підтримує це напряму.
    if db.engine.dialect.name == "postgresql":
        sponsor_col = next(
            (c for c in inspector.get_columns("voucher_redemption") if c["name"] == "sponsor_spin_id"),
            None,
        )
        if sponsor_col and not sponsor_col["nullable"]:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE voucher_redemption ALTER COLUMN sponsor_spin_id DROP NOT NULL"))
                conn.commit()


def migrate_bet_columns():
    """Видаляє застаріле поле Bet.amount — прогнози стали pool-моделлю
    без суми ставки (приз ділиться порівну між усіма, хто вгадав)."""
    inspector = inspect(db.engine)
    if "bet" not in inspector.get_table_names():
        return

    existing_cols = {col["name"] for col in inspector.get_columns("bet")}
    if "amount" in existing_cols:
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE bet DROP COLUMN amount"))
            conn.commit()


def migrate_event_columns():
    """Легка авто-міграція SQLite: додає нові nullable-колонки Event,
    якщо вони відсутні в уже існуючій таблиці (create_all їх не додає)."""
    inspector = inspect(db.engine)
    if "event" not in inspector.get_table_names():
        return

    existing_cols = {col["name"] for col in inspector.get_columns("event")}
    new_columns = {
        "sponsor_spin_id": "INTEGER",
        "prize_type": "VARCHAR(20) DEFAULT 'money'",
        "voucher_count": "INTEGER",
    }
    with db.engine.connect() as conn:
        for col_name, col_type in new_columns.items():
            if col_name not in existing_cols:
                conn.execute(text(f"ALTER TABLE event ADD COLUMN {col_name} {col_type}"))
        conn.commit()


# Ініціалізація БД/планувальника виконується на рівні модуля (а не лише
# всередині `if __name__ == "__main__":`), бо під gunicorn (Render тощо)
# файл лише імпортується як WSGI-модуль — app.run() ніколи не викликається,
# і без цього таблиці БД не створяться, а push-нагадування не запускаться.
os.makedirs(app.instance_path, exist_ok=True)

with app.app_context():
    db.create_all()
    migrate_user_columns()
    migrate_sponsor_spin_columns()
    migrate_spin_prize_columns()
    migrate_voucher_redemption_columns()
    migrate_bet_columns()
    migrate_event_columns()
    # створюємо тестового адміна, якщо його ще нема
    if not User.query.filter_by(username="admin").first():
        admin = User(username="admin", email="admin@test.com", balance=0, is_admin=True)
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()

# захист від подвійного старту планувальника під Flask debug-reloader
# (reloader спавнить дочірній процес; WERKZEUG_RUN_MAIN='true' лише в ньому)
if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    start_notification_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)
