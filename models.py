from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import secrets
import string

db = SQLAlchemy()

# (value, icon, label) — value зберігається в Event.sport_type
SPORT_CHOICES = [
    ("football", "⚽", "Футбол"),
    ("tennis", "🎾", "Теніс"),
    ("f1", "🏎️", "Формула 1"),
    ("basketball", "🏀", "Баскетбол"),
    ("hockey", "🏒", "Хокей"),
    ("boxing", "🥊", "Бокс/MMA"),
    ("esports", "🎮", "Кіберспорт"),
]
SPORT_ICONS = {value: icon for value, icon, label in SPORT_CHOICES}
SPORT_LABELS = {value: label for value, icon, label in SPORT_CHOICES}
DEFAULT_SPORT_ICON = "🎯"

# фонові зображення карток подій за видом спорту (Unsplash)
SPORT_IMAGES = {
    "football": "https://images.unsplash.com/photo-1587329310686-91414b8e3cb7?w=800&q=80",
    "tennis": "https://images.unsplash.com/photo-1542144582-1ba00456b5e3?w=800&q=80",
    "f1": "https://images.unsplash.com/photo-1659203206829-218b9b5930e5?w=800&q=80",
    "basketball": "https://images.unsplash.com/photo-1546519638-68e109498ffc?w=800&q=80",
    "hockey": "https://images.unsplash.com/photo-1545471977-94cac22e71ed?w=800&q=80",
    "boxing": "https://images.unsplash.com/photo-1549719386-74dfcbf7dbed?w=800&q=80",
    "esports": "https://images.unsplash.com/photo-1544652478-6653e09f18a2?w=800&q=80",
}
DEFAULT_SPORT_IMAGE = "https://images.unsplash.com/photo-1512144825472-b4d1e4cdeb68?w=800&q=80"


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    balance = db.Column(db.Float, default=0.0)  # віртуальний баланс
    is_admin = db.Column(db.Boolean, default=False)  # рекламодавець/адмін
    country = db.Column(db.String(80))
    phone = db.Column(db.String(30))
    is_verified = db.Column(db.Boolean, default=False)
    last_spin_at = db.Column(db.DateTime, nullable=True)  # час останньої прокрутки рулетки
    spin_ready_notified = db.Column(db.Boolean, default=False)  # чи вже надіслано push про доступність рулетки
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    sport_type = db.Column(db.String(50))
    prize_pool = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default="open")  # open / closed / settled
    match_date = db.Column(db.DateTime)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    options = db.relationship("EventOption", backref="event", lazy=True, cascade="all, delete-orphan")
    bets = db.relationship("Bet", backref="event", lazy=True, cascade="all, delete-orphan")

    @property
    def sport_icon(self):
        return SPORT_ICONS.get(self.sport_type, DEFAULT_SPORT_ICON)

    @property
    def sport_label(self):
        return SPORT_LABELS.get(self.sport_type, self.sport_type or "Інше")

    @property
    def sport_image(self):
        return SPORT_IMAGES.get(self.sport_type, DEFAULT_SPORT_IMAGE)


class EventOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False)
    option_text = db.Column(db.String(120), nullable=False)
    is_winner = db.Column(db.Boolean, default=False)

    bets = db.relationship("Bet", backref="option", lazy=True)


class Bet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False)
    option_id = db.Column(db.Integer, db.ForeignKey("event_option.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Payout(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    paid_at = db.Column(db.DateTime, default=datetime.utcnow)


class SponsorSpin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sponsor_name = db.Column(db.String(120), nullable=False)
    sponsor_logo_url = db.Column(db.String(500))
    min_reward = db.Column(db.Float, nullable=False, default=5.0)
    max_reward = db.Column(db.Float, nullable=False, default=30.0)
    is_active = db.Column(db.Boolean, default=True)
    promo_code = db.Column(db.String(50))
    promo_description = db.Column(db.String(255))
    voucher_validity_days = db.Column(db.Integer, nullable=False, default=7)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    prizes = db.relationship("SpinPrize", backref="sponsor", lazy=True, cascade="all, delete-orphan")


class SpinPrize(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sponsor_spin_id = db.Column(db.Integer, db.ForeignKey("sponsor_spin.id"), nullable=False)
    prize_type = db.Column(db.String(20), nullable=False)  # "coins" або "voucher"
    coins_amount = db.Column(db.Float)
    voucher_percent = db.Column(db.Integer)
    weight = db.Column(db.Integer, nullable=False, default=1)
    sponsor_logo_url = db.Column(db.String(500))  # лого для цього призу; якщо пусто — береться лого спонсора
    custom_label = db.Column(db.String(100))  # довільний текст на секторі (напр. "-10% на пиво 🍺"); якщо пусто — авто "-X%"


def generate_voucher_code():
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"BETFREE-{suffix}"


class VoucherRedemption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    sponsor_spin_id = db.Column(db.Integer, db.ForeignKey("sponsor_spin.id"), nullable=False)
    spin_prize_id = db.Column(db.Integer, db.ForeignKey("spin_prize.id"), nullable=True)
    unique_code = db.Column(db.String(20), unique=True, nullable=False)
    is_used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    expiry_reminder_sent = db.Column(db.Boolean, default=False)  # чи вже надіслано push про закінчення терміну

    user = db.relationship("User", backref="vouchers")
    sponsor = db.relationship("SponsorSpin", backref="voucher_redemptions")
    prize = db.relationship("SpinPrize")

    @property
    def days_left(self):
        return max(0, (self.expires_at - datetime.utcnow()).days)

    @property
    def is_expired(self):
        return not self.is_used and datetime.utcnow() >= self.expires_at

    @property
    def badge_status(self):
        if self.is_used:
            return "used"
        if self.is_expired:
            return "expired"
        if self.days_left <= 3:
            return "expiring"
        return "active"


class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    endpoint = db.Column(db.String(500), nullable=False, unique=True)
    p256dh_key = db.Column(db.String(255), nullable=False)
    auth_key = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="push_subscriptions")
