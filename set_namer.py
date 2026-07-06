"""
Creative DJ-set names via z.ai (GLM with thinking mode).

Given a generated set (tracks, genres, BPM arc, vocal profile, era), asks
GLM-4.6 — with the `thinking` parameter enabled so it can reason about the
set's mood before answering — for a short, evocative, poster-worthy name.

Fail-safe by design: no API key, timeouts, HTTP errors or unusable output all
return None, and the caller falls back to the standard auto-name. Naming must
never break playlist generation.

Setup: add to .env
    ZAI_API_KEY=...          # same account you use elsewhere
    # ZAI_MODEL=glm-4.6      # optional override

Manual test drive (names your saved playlists, prints, writes nothing):
    .venv/bin/python set_namer.py
"""
import json
import logging
import os
from typing import Optional

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

log = logging.getLogger(__name__)

# Accept both naming conventions: ZAI_* and the GLM_* names used in the
# user's other z.ai projects (oye-bot), so the same .env lines work here.
ZAI_API_KEY = os.getenv("ZAI_API_KEY") or os.getenv("GLM_API_KEY", "")
ZAI_MODEL   = os.getenv("ZAI_MODEL") or os.getenv("GLM_MODEL") or "glm-4.6"
ZAI_URL     = "https://api.z.ai/api/paas/v4/chat/completions"
# Thinking mode on a full tracklist can take 30-60 s — be generous.
ZAI_TIMEOUT = float(os.getenv("ZAI_TIMEOUT") or os.getenv("GLM_TIMEOUT") or "120")

_SYSTEM = (
    "Sos un DJ y director creativo que nombra sets para flyers de fiestas. "
    "Recibís los datos de un DJ set (tracks, géneros, arco de energía, BPM, "
    "perfil vocal, época) y devolvés UN solo nombre para el set.\n"
    "Reglas del nombre:\n"
    "- 2 a 4 palabras, evocador, con personalidad; que capture el mood real "
    "del set (usá los títulos y géneros como inspiración)\n"
    "- Puede ser en español o inglés — elegí según la vibra dominante de los "
    "temas\n"
    "- PROHIBIDO: genéricos tipo 'Deep Vibes', 'Summer Mix', 'Party Time', "
    "comillas, emojis, la palabra 'playlist', 'mix' o 'set', nombres de "
    "artistas del listado\n"
    "- Respondé SOLO con el nombre, nada más."
)


def _set_summary(playlist: dict) -> str:
    """Compact, name-worthy description of a stored playlist object."""
    tracks = playlist.get("tracks", [])
    if not tracks:
        return ""

    lines = []
    for t in tracks:
        seg = f"- {t.get('title', '?')} — {t.get('artists', '?')}"
        extras = []
        if t.get("bpm"):
            extras.append(f"{round(t['bpm'])} bpm")
        if t.get("energy") is not None:
            extras.append(f"energía {t['energy']}")
        if t.get("section_label"):
            extras.append(t["section_label"])
        if extras:
            seg += f" ({', '.join(extras)})"
        lines.append(seg)

    bpms = [t["bpm"] for t in tracks if t.get("bpm")]
    energies = [t["energy"] for t in tracks if t.get("energy") is not None]
    meta = []
    if bpms:
        meta.append(f"BPM {round(min(bpms))}–{round(max(bpms))}")
    if energies:
        meta.append(f"energía {min(energies)}→{max(energies)} (arco intro→climax→outro)")
    if playlist.get("params", {}).get("genre_filter"):
        meta.append(f"género: {playlist['params']['genre_filter']}")
    dur_min = round(playlist.get("total_duration_ms", 0) / 60000)
    if dur_min:
        meta.append(f"{dur_min} min")

    return (
        f"DJ set — {' · '.join(meta)}\n"
        f"Tracks en orden:\n" + "\n".join(lines)
    )


def _clean_name(raw: str) -> Optional[str]:
    """Sanitize the model output down to a usable set name."""
    if not raw:
        return None
    # Thinking models sometimes wrap output; keep the last non-empty line.
    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
    if not lines:
        return None
    # Strip surrounding quotes/periods/whitespace in one pass (any order),
    # then collapse internal whitespace.
    name = lines[-1].strip('"\''"“”‘’«» .\t")
    name = " ".join(name.split())
    if not name or len(name) > 60:
        return None
    words = name.split()
    if len(words) > 6:   # the model rambled — not a name
        return None
    return name


async def generate_set_name(playlist: dict) -> Optional[str]:
    """Ask GLM (thinking enabled) for a creative name. None on any failure."""
    if not ZAI_API_KEY:
        return None
    summary = _set_summary(playlist)
    if not summary:
        return None

    body = {
        "model": ZAI_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": summary},
        ],
        "thinking": {"type": "enabled"},
        "temperature": 0.9,
        "max_tokens": 4000,   # GLM-5 thinking alone can run ~2k tokens
    }
    try:
        async with httpx.AsyncClient(timeout=ZAI_TIMEOUT) as client:
            resp = await client.post(
                ZAI_URL,
                headers={"Authorization": f"Bearer {ZAI_API_KEY}"},
                json=body,
            )
        if resp.status_code != 200:
            log.warning("z.ai naming failed: %d %s", resp.status_code, resp.text[:200])
            return None
        content = resp.json()["choices"][0]["message"].get("content", "")
        name = _clean_name(content)
        if name:
            log.info("z.ai set name: %r", name)
        return name
    except Exception as e:
        log.warning("z.ai naming error: %s: %s", type(e).__name__, e)
        return None


if __name__ == "__main__":
    # Demo: propose creative names for every saved playlist (writes nothing).
    import asyncio

    async def _demo():
        from playlist_engine import load_playlists
        pls = load_playlists()
        if not pls:
            print("No hay playlists guardadas.")
            return
        for pid, pl in pls.items():
            name = await generate_set_name(pl)
            print(f"{pl.get('name', pid)!r}\n   →  {name or '(sin nombre — fallo o sin API key)'}\n")

    asyncio.run(_demo())
