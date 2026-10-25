"""TK剪辑器 Python license client. Only the public verification key ships to users."""
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import subprocess
import threading
import time
from urllib.parse import urlparse
import uuid
import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class LicenseError(RuntimeError):
    pass


class FeedbackError(RuntimeError):
    pass


def machine_code():
    """Same machine identity across all programs and virtual environments."""
    identity = None
    if platform.system() == "Windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                identity = winreg.QueryValueEx(key, "MachineGuid")[0]
        except OSError:
            pass
    elif platform.system() == "Darwin":
        try:
            output = subprocess.check_output(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], timeout=5, text=True)
            import re
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output)
            identity = match.group(1) if match else None
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        for name in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                identity = Path(name).read_text().strip()
                if identity:
                    break
            except OSError:
                pass
    if not identity:
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
        path = base / "ShaohuaLicense" / "device-id"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x") as stream:
                stream.write(str(uuid.uuid4()))
            os.chmod(path, 0o600)
        except FileExistsError:
            pass
        identity = path.read_text().strip()
        if not identity:
            raise LicenseError("设备标识文件为空，请联系管理员")
    return hashlib.sha256(("shaohua-device-v1:" + identity.strip().lower()).encode()).hexdigest()


def _decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class LicenseClient:
    def __init__(self, server_url, app_code, public_key, client_name, machine=None):
        parsed = urlparse(server_url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
            raise ValueError("授权服务器必须使用 HTTPS")
        self.server_url = server_url.rstrip("/")
        self.app_code = app_code
        self.client_name = client_name
        self.machine = machine or machine_code()
        self._public_key = Ed25519PublicKey.from_public_bytes(_decode(public_key.strip()))
        self._session = requests.Session()
        self._key = None
        self._valid_until = 0.0
        self._last_result = None
        self._stop = threading.Event()
        self._thread = None
        self._verify_lock = threading.Lock()

    def verify(self, key=None):
        """Fresh server verification. Signed rejection immediately revokes local access."""
        with self._verify_lock:
            card = key if key is not None else self._key
            if not card:
                raise LicenseError("请先输入卡密")
            if key is not None and key != self._key:
                self._valid_until = 0.0
                self._key = None
            nonce = secrets.token_urlsafe(24)
            started = time.monotonic()
            try:
                response = self._session.post(self.server_url + "/api/client/verify", json={
                    "key_code": card, "app_code": self.app_code, "machine_code": self.machine,
                    "nonce": nonce, "client_name": self.client_name,
                }, timeout=(5, 10))
            except requests.RequestException as exc:
                raise LicenseError("无法连接授权服务器，请检查网络") from exc
            try:
                envelope = response.json()
                if envelope.get("algorithm") != "Ed25519":
                    raise ValueError("Unexpected signature algorithm")
                payload = envelope["payload"]
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                self._public_key.verify(_decode(envelope["signature"]), encoded)
                expected = {"nonce": nonce, "app_code": self.app_code, "machine_code": self.machine,
                            "client_name": self.client_name, "key_fingerprint": hashlib.sha256(card.encode()).hexdigest()}
                if any(payload.get(k) != v for k, v in expected.items()):
                    raise ValueError("Response is not bound to this request")
                issued = payload["issued_at"]
                until = payload["lease_until"]
                if not isinstance(issued, int) or not isinstance(until, int) or abs(time.time() - issued) > 90:
                    raise ValueError("Stale response or incorrect system clock")
                if not 0 <= until - issued <= 900:
                    raise ValueError("Invalid lease duration")
            except (ValueError, KeyError, TypeError, AttributeError, InvalidSignature) as exc:
                self._valid_until = 0.0
                raise LicenseError("授权响应校验失败，请检查系统时间并联系管理员") from exc
            if response.status_code != 200 or payload.get("status") != "success":
                self._valid_until = 0.0
                raise LicenseError(payload.get("message", "授权失败"))
            # A monotonic deadline prevents clock rollback from extending a lease.
            ttl = min(until - issued, until - time.time()) - (time.monotonic() - started)
            if ttl <= 0:
                self._valid_until = 0.0
                raise LicenseError("授权已到期")
            self._key = card
            self._last_result = payload
            self._valid_until = time.monotonic() + ttl
            return dict(payload["key_info"])

    def require_valid(self):
        """Call at protected operations; raises on rejection or expired cached lease."""
        if time.monotonic() >= self._valid_until:
            raise LicenseError("授权已失效，请重新联网验证")

    def submit_feedback(self, app_version, description, logs_path=None, contact='', submission_id=None):
        """Submit diagnostics with a license-scoped token; never send admin secrets.

        Reuse submission_id and the same archive when retrying an uncertain result.
        Network failures do not revoke the application's existing license lease.
        """
        if not isinstance(description,str) or not 5<=len(description.strip())<=10000:
            raise FeedbackError('问题描述请填写 5–10000 个字符')
        if not isinstance(app_version,str) or not 1<=len(app_version.strip())<=64:
            raise FeedbackError('软件版本号无效')
        if not isinstance(contact,str) or len(contact)>200:raise FeedbackError('联系方式过长')
        try:ident=str(uuid.UUID(submission_id)) if submission_id else str(uuid.uuid4())
        except (ValueError,TypeError,AttributeError):raise FeedbackError('反馈提交编号无效')
        self.require_valid()
        if not (self._last_result or {}).get('feedback_token'):
            self.verify()
        with self._verify_lock:
            self.require_valid()
            token=(self._last_result or {}).get('feedback_token')
            if not token:raise FeedbackError('授权服务器尚未启用反馈接口')
            metadata=json.dumps(dict(submission_id=ident,app_version=app_version.strip(),description=description.strip(),contact=contact.strip()),ensure_ascii=False)
            stream=None
            try:
                files={'metadata':(None,metadata)}
                if logs_path is not None:
                    path=Path(logs_path)
                    if not path.is_file() or not 1<=path.stat().st_size<=15*1024*1024:
                        raise FeedbackError('日志 ZIP 不能超过 15 MB')
                    stream=path.open('rb');files['logs']=('logs.zip',stream,'application/zip')
                response=self._session.post(self.server_url+'/api/client/feedback',
                    headers={'Authorization':'Bearer '+token},files=files,timeout=(5,60))
                try:result=response.json()
                except ValueError:raise FeedbackError('反馈服务响应无效，请保留提交编号后重试')
                if response.status_code not in (200,201):
                    message=result.get('detail') or result.get('message') or '反馈提交失败'
                    raise FeedbackError(message if isinstance(message,str) else '反馈内容格式无效')
                if not isinstance(result.get('id'),str):raise FeedbackError('反馈服务响应无效')
                return result
            except requests.RequestException as exc:
                raise FeedbackError('无法提交反馈，请检查网络后重试（请保留原提交编号）') from exc
            finally:
                if stream:stream.close()

    def query_subtitles(self, series_id, sentences):
        """Batch lookup; each sentence has id/text/language and optional narrator.

        Returns evidence and language coverage. `reliable` does not replace visual
        verification. Translations must use the original sentence id and specify
        their actual language; the server does not silently translate.
        When index_ready is false, display index_progress and retry after 5 seconds.
        This pending state is not a no-match result and must not trigger visual fallback.
        """
        self.require_valid()
        if not (self._last_result or {}).get('subtitle_token'):
            self.verify()
        with self._verify_lock:
            self.require_valid()
            token=(self._last_result or {}).get('subtitle_token')
            if not token:raise LicenseError('服务器尚未启用字幕查询')
            try:
                response=self._session.post(self.server_url+'/api/client/subtitles/search',
                    headers={'Authorization':'Bearer '+token},
                    json={'series_id':series_id,'sentences':sentences},timeout=(5,60))
                result=response.json()
            except (requests.RequestException,ValueError) as exc:
                raise LicenseError('字幕查询失败，请稍后重试') from exc
            if response.status_code!=200:
                detail=result.get('detail') or result.get('message')
                raise LicenseError(detail if isinstance(detail,str) else '字幕查询失败')
            return result

    def start_heartbeat(self, interval=60, on_error=None):
        """Revalidates the license, rather than merely reporting an event."""
        self.require_valid()
        if not 10 <= interval <= 120:
            raise ValueError("心跳间隔应在 10 至 120 秒之间")
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def run():
            while not self._stop.wait(interval):
                try:
                    self.verify()
                except LicenseError as exc:
                    if on_error:
                        try:
                            on_error(exc)
                        except Exception:
                            pass
        self._thread = threading.Thread(target=run, name="license-heartbeat", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread and threading.current_thread() is not self._thread:
            self._thread.join(timeout=16)
        self._valid_until = 0.0
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
