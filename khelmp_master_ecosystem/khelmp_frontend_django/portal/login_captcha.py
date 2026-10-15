import base64
import hashlib
import hmac
import io
import secrets
import string

from django.conf import settings
from PIL import Image, ImageDraw, ImageFont


SESSION_KEY = 'login_captcha_challenge'
SIGNUP_SESSION_KEY = 'signup_captcha_challenge'


def _signature(nonce, code):
    message = f'{nonce}:{code}'.encode('utf-8')
    return hmac.new(settings.SECRET_KEY.encode('utf-8'), message, hashlib.sha256).hexdigest()


def issue_image_captcha(request, session_key=SESSION_KEY):
    code = ''.join(secrets.choice(string.digits) for _ in range(6))
    nonce = secrets.token_urlsafe(24)
    request.session[session_key] = {
        'nonce': nonce,
        'signature': _signature(nonce, code),
    }

    image = Image.new('RGB', (220, 72), '#f2f6f3')
    draw = ImageDraw.Draw(image)
    rng = secrets.SystemRandom()
    for _ in range(9):
        draw.line(
            (
                rng.randrange(image.width),
                rng.randrange(image.height),
                rng.randrange(image.width),
                rng.randrange(image.height),
            ),
            fill=rng.choice(('#9bb4a8', '#b6a79a', '#a6b6c0')),
            width=rng.choice((1, 2)),
        )
    for _ in range(45):
        x, y = rng.randrange(image.width), rng.randrange(image.height)
        draw.point((x, y), fill=rng.choice(('#879b91', '#b3a89e', '#8b9eaa')))

    try:
        font = ImageFont.load_default(size=38)
    except TypeError:
        font = ImageFont.load_default()
    bounds = draw.textbbox((0, 0), code, font=font, stroke_width=1)
    text_width = bounds[2] - bounds[0]
    text_height = bounds[3] - bounds[1]
    draw.text(
        ((image.width - text_width) / 2, (image.height - text_height) / 2 - bounds[1]),
        code,
        fill='#173f35',
        font=font,
        stroke_width=1,
        stroke_fill='#173f35',
    )

    output = io.BytesIO()
    image.save(output, format='PNG', optimize=True)
    image_data = base64.b64encode(output.getvalue()).decode('ascii')
    return f'data:image/png;base64,{image_data}'


def consume_image_captcha(request, submitted_code, session_key=SESSION_KEY):
    challenge = request.session.pop(session_key, None)
    if not isinstance(challenge, dict):
        return False

    nonce = challenge.get('nonce')
    signature = challenge.get('signature')
    if not isinstance(nonce, str) or not isinstance(signature, str):
        return False

    expected_signature = _signature(nonce, (submitted_code or '').strip())
    return hmac.compare_digest(signature, expected_signature)
