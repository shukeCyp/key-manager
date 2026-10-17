from urllib.parse import unquote
import pyotp
from app.config import settings
from test_licensing import env, admin_headers, generate, raw_verify


def test_tk_single_application_and_legacy_client_compatibility(env):
    client, _ = env
    apps = client.get('/api/applications', headers=admin_headers(client)).json()
    assert len(apps) == 1
    assert apps[0]['name'] == 'TK剪辑器'
    assert apps[0]['app_code'] == 'TK_EDITOR'
    card = generate(env)
    assert raw_verify(client, card, app_code='SHAOHUA_SUITE').status_code == 200
    assert raw_verify(client, card, app_code='TK_EDITOR').status_code == 200


def test_admin_totp_setup_login_and_disable(env):
    client, _ = env
    headers = admin_headers(client)
    setup = client.post('/api/settings/otp-setup', headers=headers).json()
    assert 'TK剪辑器' in unquote(setup['otp_uri'])
    assert setup['qr_code'].startswith('data:image/png;base64,')
    code = pyotp.TOTP(setup['otp_secret']).now()
    assert client.post('/api/settings/otp-verify-setup', headers=headers,
                       json={'code': code, 'secret': setup['otp_secret']}).status_code == 200
    login = {'username':'admin','password':settings.ADMIN_PASSWORD,'captcha_code':'','captcha_key':''}
    assert client.post('/api/auth/login', json=login).status_code == 401
    assert client.post('/api/auth/login', json={**login,'otp_code':code}).status_code == 200
    assert client.post('/api/settings/otp-disable', headers=headers,json={'code':code}).status_code == 200
