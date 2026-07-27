#!/usr/bin/env python3
"""
bounty-watch — theo dõi arkadiyt/bounty-targets-data, phát hiện program/scope mới
và bắn thông báo về Telegram.

Chạy: python watcher.py            (dùng biến môi trường)
      python watcher.py --dry-run  (in ra stdout, không gửi Telegram)
      python watcher.py --bootstrap (chỉ tạo baseline, không gửi gì)

Biến môi trường bắt buộc (trừ khi --dry-run):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
Tuỳ chọn:
    PLATFORMS      mặc định "intigriti,hackerone"
    MIN_SCORE      chỉ báo program mới có điểm >= giá trị này (mặc định 0 = báo hết)
    STATE_DIR      mặc định "./state"
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

BASE = "https://raw.githubusercontent.com/arkadiyt/bounty-targets-data/main/data"

PLATFORM_FILES = {
    "intigriti": "intigriti_data.json",
    "hackerone": "hackerone_data.json",
    "bugcrowd": "bugcrowd_data.json",
    "yeswehack": "yeswehack_data.json",
    "federacy": "federacy_data.json",
}

TELEGRAM_MAX = 3900  # giới hạn thật là 4096, chừa chỗ an toàn


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class Program:
    key: str                      # định danh ổn định, dùng để so sánh giữa 2 lần chạy
    platform: str
    name: str
    url: str
    is_vdp: bool
    min_bounty: float = 0.0
    max_bounty: float = 0.0
    currency: str = ""
    barriers: list[str] = field(default_factory=list)
    in_scope: list[str] = field(default_factory=list)
    wildcards: int = 0

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "Program":
        return Program(**d)


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def fetch_json(url: str, retries: int = 3) -> list | dict:
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "bounty-watch/1.0 (team internal)"}
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"fetch thất bại sau {retries} lần: {url} ({last_err})")


# --------------------------------------------------------------------------
# Normalizer — mỗi platform có shape JSON khác nhau
# --------------------------------------------------------------------------

def _scope_strings(entries: list, *keys: str) -> tuple[list[str], int]:
    """Rút endpoint ra khỏi list scope, đếm số wildcard."""
    out, wild = [], 0
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        val = next((str(e[k]) for k in keys if e.get(k)), None)
        if not val:
            continue
        out.append(val)
        etype = str(e.get("type") or e.get("asset_type") or "").lower()
        if "wildcard" in etype or val.startswith("*."):
            wild += 1
    return sorted(set(out)), wild


def norm_intigriti(raw: list) -> dict[str, Program]:
    progs = {}
    for p in raw:
        if p.get("status") != "open" or p.get("confidentiality_level") != "public":
            continue
        scope, wild = _scope_strings(
            (p.get("targets") or {}).get("in_scope"), "endpoint"
        )
        maxb = float((p.get("max_bounty") or {}).get("value") or 0)
        minb = float((p.get("min_bounty") or {}).get("value") or 0)
        barriers = []
        if p.get("twoFactorRequired"):
            barriers.append("2FA")
        if p.get("tacRequired"):
            barriers.append("T&C")
        key = f"intigriti:{p.get('company_handle')}/{p.get('handle')}"
        progs[key] = Program(
            key=key,
            platform="intigriti",
            name=p.get("name") or p.get("handle") or "?",
            url=p.get("url") or "",
            is_vdp=(maxb == 0),
            min_bounty=minb,
            max_bounty=maxb,
            currency=(p.get("max_bounty") or {}).get("currency") or "",
            barriers=barriers,
            in_scope=scope,
            wildcards=wild,
        )
    return progs


def norm_hackerone(raw: list) -> dict[str, Program]:
    progs = {}
    for p in raw:
        if p.get("submission_state") not in (None, "open"):
            continue
        scope, wild = _scope_strings(
            (p.get("targets") or {}).get("in_scope"),
            "asset_identifier", "target", "endpoint",
        )
        offers = bool(p.get("offers_bounties"))
        key = f"hackerone:{p.get('handle')}"
        progs[key] = Program(
            key=key,
            platform="hackerone",
            name=p.get("name") or p.get("handle") or "?",
            url=p.get("url") or f"https://hackerone.com/{p.get('handle')}",
            is_vdp=not offers,
            in_scope=scope,
            wildcards=wild,
        )
    return progs


def norm_generic(platform: str) -> callable:
    """Fallback cho bugcrowd / yeswehack / federacy.

    CẢNH BÁO: tên field của các platform này CHƯA được verify.
    Chạy --dry-run và kiểm tra output trước khi bật production.
    """
    def _norm(raw: list) -> dict[str, Program]:
        progs = {}
        for p in raw:
            if not isinstance(p, dict):
                continue
            name = p.get("name") or p.get("title") or p.get("slug") or "?"
            url = p.get("url") or p.get("program_url") or ""
            scope, wild = _scope_strings(
                (p.get("targets") or {}).get("in_scope"),
                "target", "endpoint", "asset_identifier", "uri", "name",
            )
            maxb = p.get("max_payout") or p.get("max_bounty") or 0
            if isinstance(maxb, dict):
                maxb = maxb.get("value") or 0
            key = f"{platform}:{url or name}"
            progs[key] = Program(
                key=key,
                platform=platform,
                name=name,
                url=url,
                is_vdp=(float(maxb or 0) == 0),
                max_bounty=float(maxb or 0),
                in_scope=scope,
                wildcards=wild,
            )
        return progs
    return _norm


NORMALIZERS = {
    "intigriti": norm_intigriti,
    "hackerone": norm_hackerone,
    "bugcrowd": norm_generic("bugcrowd"),
    "yeswehack": norm_generic("yeswehack"),
    "federacy": norm_generic("federacy"),
}


# --------------------------------------------------------------------------
# Chấm điểm mức độ phù hợp với người mới (rubric của nhóm)
# --------------------------------------------------------------------------

def score_newbie(p: Program) -> tuple[int, list[str]]:
    """Trả về (điểm 0-10, danh sách lý do). Càng cao càng hợp người mới."""
    score, why = 0, []

    # C1 — rào cản tham gia
    if not p.barriers:
        score += 2
        why.append("không rào cản tham gia")
    elif len(p.barriers) == 1:
        score += 1
        why.append(f"cần {p.barriers[0]}")
    else:
        why.append(f"cần {', '.join(p.barriers)}")

    # C2 — độ rộng scope
    n = len(p.in_scope)
    if p.wildcards > 0:
        score += 3
        why.append(f"{p.wildcards} wildcard")
    elif n >= 15:
        score += 3
        why.append(f"{n} asset")
    elif n >= 5:
        score += 2
        why.append(f"{n} asset")
    elif n >= 1:
        score += 1
        why.append(f"{n} asset")

    # C3 — mức cạnh tranh (suy luận: VDP ít người cày hơn)
    if p.is_vdp:
        score += 3
        why.append("VDP — ít cạnh tranh")
    elif p.max_bounty and p.max_bounty <= 2000:
        score += 2
        why.append("bounty thấp — ít cạnh tranh")
    else:
        score += 1

    # C4 — có mobile/API/nhiều loại asset thì bề mặt đa dạng hơn
    if any("*." in s for s in p.in_scope):
        score += 1

    return min(score, 10), why


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------

@dataclass
class Change:
    kind: str          # new_program | removed_program | scope_added | bounty_changed
    program: Program
    detail: list[str] = field(default_factory=list)


def diff(old: dict[str, Program], new: dict[str, Program]) -> list[Change]:
    changes = []

    for key, p in new.items():
        if key not in old:
            changes.append(Change("new_program", p))
            continue

        o = old[key]
        added = sorted(set(p.in_scope) - set(o.in_scope))
        if added:
            changes.append(Change("scope_added", p, added))

        if (o.max_bounty or 0) != (p.max_bounty or 0):
            changes.append(
                Change("bounty_changed", p,
                       [f"{o.max_bounty:g} → {p.max_bounty:g} {p.currency}"])
            )

    for key, o in old.items():
        if key not in new:
            changes.append(Change("removed_program", o))

    return changes


# --------------------------------------------------------------------------
# Định dạng thông báo (Telegram HTML — an toàn hơn MarkdownV2 nhiều)
# --------------------------------------------------------------------------

def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def render(c: Change) -> str:
    p = c.program
    plat = p.platform.upper()

    if c.kind == "new_program":
        score, why = score_newbie(p)
        stars = "🟢" if score >= 8 else ("🟡" if score >= 5 else "⚪")
        lines = [
            f"{stars} <b>PROGRAM MỚI</b> · {esc(plat)}",
            f"<b>{esc(p.name)}</b>",
            f"Điểm newbie: <b>{score}/10</b> — {esc(', '.join(why))}",
        ]
        if p.max_bounty:
            lines.append(f"Bounty: {p.min_bounty:g}–{p.max_bounty:g} {esc(p.currency)}")
        else:
            lines.append("Bounty: VDP / không thưởng tiền")
        if p.in_scope:
            preview = p.in_scope[:8]
            lines.append("Scope: <code>" + esc(", ".join(preview)) + "</code>"
                         + (f" … (+{len(p.in_scope) - 8})" if len(p.in_scope) > 8 else ""))
        if p.url:
            lines.append(f'<a href="{esc(p.url)}">Mở program</a>')
        return "\n".join(lines)

    if c.kind == "scope_added":
        preview = c.detail[:12]
        more = f" … (+{len(c.detail) - 12})" if len(c.detail) > 12 else ""
        return (
            f"🔵 <b>SCOPE MỞ RỘNG</b> · {esc(plat)}\n"
            f"<b>{esc(p.name)}</b> — thêm {len(c.detail)} asset\n"
            f"<code>{esc(', '.join(preview))}</code>{more}\n"
            f'<a href="{esc(p.url)}">Mở program</a>'
        )

    if c.kind == "bounty_changed":
        return (
            f"💰 <b>ĐỔI MỨC BOUNTY</b> · {esc(plat)}\n"
            f"<b>{esc(p.name)}</b>: {esc(c.detail[0])}\n"
            f'<a href="{esc(p.url)}">Mở program</a>'
        )

    return f"⚫ <b>PROGRAM ĐÓNG</b> · {esc(plat)}\n<b>{esc(p.name)}</b>"


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram(token: str, chat_id: str, text: str) -> None:
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        print(f"[telegram] HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)


def batch(blocks: list[str], limit: int = TELEGRAM_MAX) -> list[str]:
    """Gộp nhiều block thành ít message nhất có thể mà không vượt giới hạn."""
    out, cur = [], ""
    for b in blocks:
        if len(b) > limit:                      # block đơn lẻ đã quá dài
            if cur:
                out.append(cur); cur = ""
            out.append(b[:limit])
            continue
        if len(cur) + len(b) + 2 > limit:
            out.append(cur); cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Program]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: Program.from_json(v) for k, v in data.items()}


def save_state(path: Path, progs: dict[str, Program]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({k: v.to_json() for k, v in progs.items()},
                   ensure_ascii=False, sort_keys=True, indent=1),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="in ra stdout, không gửi")
    ap.add_argument("--bootstrap", action="store_true", help="chỉ lưu baseline")
    args = ap.parse_args()

    platforms = [p.strip() for p in
                 os.getenv("PLATFORMS", "intigriti,hackerone").split(",") if p.strip()]
    min_score = int(os.getenv("MIN_SCORE", "0"))
    state_dir = Path(os.getenv("STATE_DIR", "state"))

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    live = not (args.dry_run or args.bootstrap)
    if live and not (token and chat_id):
        print("Thiếu TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2

    all_blocks: list[str] = []

    for plat in platforms:
        fname = PLATFORM_FILES.get(plat)
        if not fname:
            print(f"[warn] không biết platform '{plat}', bỏ qua", file=sys.stderr)
            continue

        try:
            raw = fetch_json(f"{BASE}/{fname}")
        except RuntimeError as e:
            print(f"[error] {plat}: {e}", file=sys.stderr)
            continue

        new = NORMALIZERS[plat](raw)
        state_path = state_dir / f"{plat}.json"
        first_run = not state_path.exists()
        old = load_state(state_path)

        if first_run or args.bootstrap:
            save_state(state_path, new)
            print(f"[{plat}] baseline: {len(new)} program (không gửi thông báo)")
            continue

        changes = diff(old, new)

        # lọc theo điểm — chỉ áp dụng cho program mới
        kept = []
        for c in changes:
            if c.kind == "new_program":
                s, _ = score_newbie(c.program)
                if s < min_score:
                    continue
            kept.append(c)

        # program mới lên đầu, rồi scope mở rộng, rồi phần còn lại
        order = {"new_program": 0, "scope_added": 1, "bounty_changed": 2,
                 "removed_program": 3}
        kept.sort(key=lambda c: (order.get(c.kind, 9), c.program.name))

        print(f"[{plat}] {len(new)} program, {len(kept)} thay đổi đáng báo")
        all_blocks.extend(render(c) for c in kept)

        save_state(state_path, new)

    if not all_blocks:
        print("Không có gì mới.")
        return 0

    messages = batch(all_blocks)
    for i, msg in enumerate(messages):
        if live:
            send_telegram(token, chat_id, msg)
            time.sleep(1.2)          # tôn trọng rate limit của Telegram
        else:
            print(f"\n----- message {i + 1}/{len(messages)} -----\n{msg}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
