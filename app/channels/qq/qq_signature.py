from app.channels.qq.qq_config import QQConfig


def repeat_seed(secret: str, target_size: int = 32) -> bytes:
    seed = secret or ""
    while len(seed) < target_size:
        seed += seed
    return seed[:target_size].encode("utf-8")


def sign_webhook_validation(config: QQConfig, event_ts: str, plain_token: str) -> str:
    if not config.bot_secret:
        raise ValueError("Missing QQ_BOT_SECRET; cannot verify QQ webhook callback")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'cryptography'; install it to enable QQ webhook validation"
        ) from exc

    private_key = Ed25519PrivateKey.from_private_bytes(repeat_seed(config.bot_secret))
    return private_key.sign(f"{event_ts}{plain_token}".encode("utf-8")).hex()


def build_webhook_validation_response(payload: dict, config: QQConfig) -> dict:
    data = payload.get("d") or {}
    plain_token = str(data.get("plain_token") or "")
    event_ts = str(data.get("event_ts") or "")
    if not plain_token or not event_ts:
        raise ValueError("QQ webhook validation payload missing plain_token or event_ts")
    return {
        "plain_token": plain_token,
        "signature": sign_webhook_validation(config, event_ts, plain_token),
    }
