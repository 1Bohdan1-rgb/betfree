"""
Одноразовий скрипт генерації VAPID-ключів для Web Push.

Запуск:  python generate_vapid_keys.py

Ключі зберігаються в instance/ (не в git):
  - instance/vapid_private.pem   — приватний ключ (для підпису push-повідомлень на бекенді)
  - instance/vapid_public_key.txt — публічний ключ у форматі base64url
                                     (applicationServerKey для фронтенду)
"""
import os
import sys
import base64
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

INSTANCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance")
PRIVATE_KEY_PATH = os.path.join(INSTANCE_DIR, "vapid_private.pem")
PUBLIC_KEY_PATH = os.path.join(INSTANCE_DIR, "vapid_public_key.txt")


def main():
    os.makedirs(INSTANCE_DIR, exist_ok=True)

    if os.path.exists(PRIVATE_KEY_PATH):
        print(f"VAPID-ключі вже існують: {PRIVATE_KEY_PATH}")
        print("Видаліть файл вручну, якщо хочете згенерувати нові.")
        return

    vapid = Vapid()
    vapid.generate_keys()
    vapid.save_key(PRIVATE_KEY_PATH)

    raw_public = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_key_b64 = base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode("utf-8")

    with open(PUBLIC_KEY_PATH, "w") as f:
        f.write(public_key_b64)

    print("VAPID-ключі згенеровано:")
    print(f"  Приватний ключ: {PRIVATE_KEY_PATH}")
    print(f"  Публічний ключ: {PUBLIC_KEY_PATH}")
    print(f"  Public key (base64url, для фронтенду): {public_key_b64}")


if __name__ == "__main__":
    main()
