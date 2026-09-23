"""Single-worker configuration with locked, atomic dotenv transactions."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

from dotenv.parser import parse_stream
from filelock import FileLock, Timeout

from gateway.errors import GatewayError

# Only the session-cookie family observed on chat.dialx.ai is accepted.
SESSION_COOKIE = "__Secure-next-auth.session-token"
_COOKIE_NAME = re.compile(re.escape(SESSION_COOKIE) + r"(?:\.(0|[1-9][0-9]*))?\Z")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]+\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
T = TypeVar("T")


@dataclass(frozen=True, repr=False)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    api_token: str = "123456"
    default_model: str = ""
    model_cache_ttl: int = 3600
    upstream_timeout: float = 1800
    ignore_unsupported_params: bool = False
    connect_timeout: float = 15
    cooldown_seconds: int = 60
    account_wait_seconds: float = 2
    log_retention_days: int = 7
    max_frame_bytes: int = 2097152
    max_response_bytes: int = 16777216
    data_dir: Path = Path("data")
    enabled_toolsets: tuple[str, ...] = ()


_DEFAULTS = {
    "HOST": "127.0.0.1", "PORT": "8000", "API_TOKEN": "123456", "DEFAULT_MODEL": "",
    "MODEL_CACHE_TTL": "3600", "UPSTREAM_TIMEOUT": "1800", "CONNECT_TIMEOUT": "15",
    "IGNORE_UNSUPPORTED_PARAMS": "false",
    "COOLDOWN_SECONDS": "60", "ACCOUNT_WAIT_SECONDS": "2", "LOG_RETENTION_DAYS": "7",
    "MAX_FRAME_BYTES": "2097152", "MAX_RESPONSE_BYTES": "16777216", "DATA_DIR": "data",
    "DIALX_ENABLED_TOOLSETS": "[]", "DIALX_COOKIES": "", "DIALX_ACCOUNTS": "[]",
    "COGITO_DISABLED": "[]",
}


def _invalid(message: str = "配置格式无效。") -> GatewayError:
    return GatewayError(400, "invalid_config", message)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1].strip()
    return text


def normalize_cookie(text: str, *, index: int = 1) -> str:
    """Normalize one browser Cookie header, never include its contents in errors."""
    def bad(reason: str) -> GatewayError:
        return GatewayError(400, "invalid_cookie", f"第 {index} 条 Cookie：{reason}")

    if not isinstance(text, str):
        raise bad("格式无效。")
    text = _unquote(text)
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    if any(c in text for c in "\r\n\x00"):
        raise bad("不允许控制字符。")
    tokens: dict[str, str] = {}
    for part in text.split(";"):
        name, separator, value = part.strip().partition("=")
        if not name.startswith(SESSION_COOKIE):
            continue
        if not separator or not _COOKIE_NAME.fullmatch(name):
            raise bad("会话分块名称无效。")
        if not _COOKIE_VALUE.fullmatch(value):
            raise bad("会话值为空或含非法字符。")
        if name in tokens and tokens[name] != value:
            raise bad("同名会话分块冲突。")
        tokens[name] = value
    if not tokens:
        raise bad("未找到受支持的 NextAuth 会话 Cookie。")
    if SESSION_COOKIE in tokens:
        if len(tokens) != 1:
            raise bad("完整会话与分块不能混用。")
        return f"{SESSION_COOKIE}={tokens[SESSION_COOKIE]}"
    try:
        chunks = sorted((int(name.rsplit(".", 1)[1]), value) for name, value in tokens.items())
    except ValueError:
        raise bad("会话分块编号无效。") from None
    if [number for number, _ in chunks] != list(range(len(chunks))):
        raise bad("会话分块必须从 0 连续编号，不能缺块。")
    return "; ".join(f"{SESSION_COOKIE}.{number}={value}" for number, value in chunks)


def _cookies(text: str) -> list[str]:
    if not isinstance(text, str):
        raise GatewayError(400, "invalid_cookie", "Cookie 导入必须是文本。")
    lines = re.split(r"\r?\n|\|\|\|", _unquote(text))
    return [normalize_cookie(line, index=i) for i, line in enumerate(lines, 1) if line.strip()]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_json(raw: str, key: str) -> object:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        raise _invalid(f"{key} 必须是有效的 JSON。") from None


def _settings(values: dict[str, str], parent: Path) -> Settings:
    def number(key: str, minimum: float, *, integer: bool = True) -> int | float:
        try:
            result = int(values[key]) if integer else float(values[key])
            if not math.isfinite(result) or result < minimum:
                raise ValueError
            return result
        except (ValueError, OverflowError):
            raise _invalid(f"{key} 数值无效。") from None

    token = values["API_TOKEN"]
    if not token or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token):
        raise _invalid("API_TOKEN 必须是一个非空、无空白的 Key。")
    if any(separator in token for separator in (",", ";", "|")):
        raise _invalid("API_TOKEN 只支持一个 Key，不接受列表分隔符。")
    if not values["HOST"].strip() or any(c.isspace() for c in values["HOST"]):
        raise _invalid("HOST 格式无效。")
    port = number("PORT", 1)
    if port > 65535:
        raise _invalid("PORT 必须在 1 到 65535 之间。")
    tools = _parse_json(values["DIALX_ENABLED_TOOLSETS"], "DIALX_ENABLED_TOOLSETS")
    if tools != []:
        raise GatewayError(400, "unverified_tools", "尚未验证工具绑定；工具集必须为空。")
    if "\x00" in values["DATA_DIR"] or not values["DATA_DIR"].strip():
        raise _invalid("DATA_DIR 格式无效。")
    data_dir = Path(values["DATA_DIR"]).expanduser()
    if not data_dir.is_absolute():
        data_dir = parent / data_dir
    frame = number("MAX_FRAME_BYTES", 1)
    response = number("MAX_RESPONSE_BYTES", 1)
    if response < frame:
        raise _invalid("MAX_RESPONSE_BYTES 不能小于 MAX_FRAME_BYTES。")
    compatibility = values["IGNORE_UNSUPPORTED_PARAMS"].strip().lower()
    if compatibility not in {"true", "false"}:
        raise _invalid("IGNORE_UNSUPPORTED_PARAMS 必须是 true 或 false。")
    return Settings(
        host=values["HOST"], port=port, api_token=token, default_model=values["DEFAULT_MODEL"],
        model_cache_ttl=number("MODEL_CACHE_TTL", 0),
        upstream_timeout=number("UPSTREAM_TIMEOUT", 0.001, integer=False),
        ignore_unsupported_params=compatibility == "true",
        connect_timeout=number("CONNECT_TIMEOUT", 0.001, integer=False),
        cooldown_seconds=number("COOLDOWN_SECONDS", 0),
        account_wait_seconds=number("ACCOUNT_WAIT_SECONDS", 0, integer=False),
        log_retention_days=number("LOG_RETENTION_DAYS", 1),
        max_frame_bytes=frame, max_response_bytes=response, data_dir=data_dir,
    )


class ConfigStore:
    """Never publish a mutation until its complete dotenv file is on disk."""

    def __init__(self, path: Path):
        self.path = Path(path).absolute()
        self._lock = threading.RLock()
        self._file_lock = FileLock(str(self.path) + ".lock", timeout=10)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise GatewayError(507, "config_persistence_failed", "无法创建配置目录。") from None
        self._transaction(lambda values, accounts, disabled: None, initialize=True)

    # Published snapshots are never mutated; readers must not wait for file I/O.
    @property
    def settings(self) -> Settings:
        return self._state[0]

    def accounts(self) -> list[dict]:
        return [dict(record) for record in self._state[1]]

    @property
    def disabled_ids(self) -> set[str]:
        return set(self._state[2])

    def _read(self, initialize: bool) -> tuple[dict[str, str], list]:
        try:
            text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        except (OSError, UnicodeError):
            raise GatewayError(507, "config_read_failed", "无法读取配置文件。") from None
        bindings = list(parse_stream(io.StringIO(text)))
        if any(binding.error for binding in bindings):
            raise _invalid("配置文件含无法解析的行。")
        values = dict(_DEFAULTS)
        if initialize:
            values.update({key: os.environ[key] for key in values if key in os.environ})
        for binding in bindings:
            if binding.key in values:
                values[binding.key] = binding.value if binding.value is not None else ""
        return values, bindings

    @staticmethod
    def _records(values: dict[str, str]) -> tuple[list[dict], set[str]]:
        accounts = _parse_json(values["DIALX_ACCOUNTS"], "DIALX_ACCOUNTS")
        disabled = _parse_json(values["COGITO_DISABLED"], "COGITO_DISABLED")
        if not isinstance(accounts, list) or not isinstance(disabled, list):
            raise _invalid("账号记录与停用 ID 必须是 JSON 数组。")
        seen = set()
        records = []
        for record in accounts:
            if not isinstance(record, dict) or set(record) != {"id", "generation", "cookie", "created_at"}:
                raise _invalid("账号记录字段无效。")
            if any(not isinstance(record[key], str) for key in record):
                raise _invalid("账号记录字段类型无效。")
            if not all(_IDENTIFIER.fullmatch(record[key]) for key in ("id", "generation")):
                raise _invalid("账号 ID 或代次无效。")
            if record["id"] in seen:
                raise _invalid("账号 ID 重复。")
            try:
                created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
                if created.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise _invalid("账号创建时间无效。") from None
            seen.add(record["id"])
            records.append({**record, "cookie": normalize_cookie(record["cookie"])})
        if any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in disabled):
            raise _invalid("停用 ID 格式无效。")
        return records, set(disabled)

    @staticmethod
    def _insert(accounts: list[dict], disabled: set[str], cookies: list[str], enabled: bool) -> dict:
        hashes = {hashlib.sha256(record["cookie"].encode()).hexdigest() for record in accounts}
        ids = {record["id"] for record in accounts}
        added = []
        duplicates = 0
        for cookie in cookies:
            digest = hashlib.sha256(cookie.encode()).hexdigest()
            if digest in hashes:
                duplicates += 1
                continue
            account_id = "acc_" + digest[:16]
            if account_id in ids:
                account_id = "acc_" + uuid.uuid4().hex[:16]
            accounts.append({
                "id": account_id, "generation": uuid.uuid4().hex, "cookie": cookie,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            ids.add(account_id)
            hashes.add(digest)
            added.append(account_id)
            if not enabled:
                disabled.add(account_id)
        return {"added": len(added), "duplicates": duplicates, "ids": added}

    def _write(self, values: dict[str, str], bindings: list) -> None:
        # Preserve unrelated values and comments verbatim, including multiline values.
        text = "".join(b.original.string for b in bindings if b.key not in _DEFAULTS)
        if text and not text.endswith("\n"):
            text += "\n"
        for key in _DEFAULTS:
            escaped = values[key].replace("\\", "\\\\").replace("'", "\\'")
            text += f"{key}='{escaped}'\n"
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def _transaction(self, change: Callable[..., T], *, initialize: bool = False) -> T:
        with self._lock:
            try:
                with self._file_lock:
                    values, bindings = self._read(initialize)
                    original_values = dict(values)
                    accounts, disabled = self._records(values)
                    imports = _cookies(values["DIALX_COOKIES"])
                    self._insert(accounts, disabled, imports, True)
                    result = change(values, accounts, disabled)
                    disabled.intersection_update(record["id"] for record in accounts)
                    settings = _settings(values, self.path.parent)
                    values["DIALX_COOKIES"] = ""
                    values["DIALX_ACCOUNTS"] = _json(accounts)
                    values["COGITO_DISABLED"] = _json(sorted(disabled))
                    if initialize or values != original_values:
                        self._write(values, bindings)
                    self._state = (settings, accounts, frozenset(disabled))
                    return result
            except (OSError, UnicodeError, Timeout):
                raise GatewayError(507, "config_persistence_failed", "配置未保存；请检查磁盘权限和可用空间。") from None

    def add_accounts(self, text: str, enabled: bool = True) -> dict:
        cookies = _cookies(text)
        if not cookies:
            raise GatewayError(400, "invalid_cookie", "请输入至少一条 Cookie。")
        if not isinstance(enabled, bool):
            raise _invalid("账号启用状态必须是布尔值。")
        return self._transaction(lambda values, accounts, disabled: self._insert(accounts, disabled, cookies, enabled))

    def delete_accounts(self, ids: list[str]) -> int:
        selected = set(ids)

        def change(values, accounts, disabled):
            original = len(accounts)
            accounts[:] = [record for record in accounts if record["id"] not in selected]
            disabled.difference_update(selected)
            return original - len(accounts)

        return self._transaction(change)

    def set_enabled(self, account_id: str, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise _invalid("账号启用状态必须是布尔值。")

        def change(values, accounts, disabled):
            if not any(record["id"] == account_id for record in accounts):
                raise GatewayError(404, "account_not_found", "账号不存在。")
            if enabled:
                disabled.discard(account_id)
            else:
                disabled.add(account_id)

        self._transaction(change)

    def refresh_cookie(self, account_id: str, generation: str, cookie: str) -> bool:
        def change(values, accounts, disabled):
            for record in accounts:
                if record["id"] == account_id and record["generation"] == generation:
                    try:
                        record["cookie"] = normalize_cookie(cookie)
                    except GatewayError:
                        raise GatewayError(502, "invalid_cookie", "上游会话 Cookie 已失效，请重新导入。", account_id=account_id) from None
                    return True
            return False

        return self._transaction(change)

    def set_default_model(self, model_id: str) -> None:
        if not isinstance(model_id, str) or "\x00" in model_id:
            raise _invalid("默认模型必须是文本。")
        self._transaction(lambda values, accounts, disabled: values.__setitem__("DEFAULT_MODEL", model_id))

    def secret_values(self) -> list[str]:
        settings, accounts, _ = self._state
        secrets = {settings.api_token}
        for record in accounts:
            secrets.add(record["cookie"])
            values = [part.split("=", 1)[1] for part in record["cookie"].split("; ")]
            secrets.update(values)
            secrets.add("".join(values))
        return sorted(secrets, key=len, reverse=True)
