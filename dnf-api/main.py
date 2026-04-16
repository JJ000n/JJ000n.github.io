"""
DNF 캐릭터 시트 API
Neople Open API를 활용해 캐릭터 장비/아바타/크리처/스탯 정보를 조회합니다.
"""

import asyncio
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="DNF Character Sheet API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://jj000n.github.io",
        "http://localhost:1313",
        "http://localhost:3000",
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Constants ─────────────────────────────────────────────────────────────────
NEOPLE_BASE = "https://api.neople.co.kr/df"

SERVER_MAP: dict[str, str] = {
    "카인": "cain",
    "디레지에": "diregie",
    "시로코": "siroco",
    "바칼": "bakal",
    "하자드": "hilder",
    "안톤": "anton",
    "카시야스": "kasias",
}

# 마법부여 스탯 점수표 (Apps Script '마법부여 기준' 시트 기반)
# 점수가 높을수록 좋은 마법부여
ENCHANT_SCORE: dict[str, dict] = {
    # 물리/마법 공격력
    "물리 공격력": {"score": 5, "per_value": 1},
    "마법 공격력": {"score": 5, "per_value": 1},
    # 힘/지능/체력/정신력
    "힘": {"score": 3, "per_value": 1},
    "지능": {"score": 3, "per_value": 1},
    "체력": {"score": 2, "per_value": 1},
    "정신력": {"score": 2, "per_value": 1},
    # 속도
    "공격 속도": {"score": 4, "per_value": 1},
    "캐스팅 속도": {"score": 4, "per_value": 1},
    "이동 속도": {"score": 2, "per_value": 1},
    # 속성 강화
    "화속성 강화": {"score": 4, "per_value": 1},
    "수속성 강화": {"score": 4, "per_value": 1},
    "명속성 강화": {"score": 4, "per_value": 1},
    "암속성 강화": {"score": 4, "per_value": 1},
    "화속성 저항": {"score": 2, "per_value": 1},
    "수속성 저항": {"score": 2, "per_value": 1},
    "명속성 저항": {"score": 2, "per_value": 1},
    "암속성 저항": {"score": 2, "per_value": 1},
    # 크리티컬
    "크리티컬 히트": {"score": 4, "per_value": 1},
    # HP/MP
    "HP": {"score": 1, "per_value": 100},
    "MP": {"score": 1, "per_value": 100},
    "HP MAX": {"score": 1, "per_value": 100},
    "MP MAX": {"score": 1, "per_value": 100},
    # 쿨타임
    "스킬 쿨타임 감소": {"score": 6, "per_value": 1},
    # 회피/명중
    "회피율": {"score": 2, "per_value": 1},
    "명중률": {"score": 2, "per_value": 1},
    # 방어력
    "물리 방어": {"score": 1, "per_value": 10},
    "마법 방어": {"score": 1, "per_value": 10},
}

# ── Item detail cache (7일) ────────────────────────────────────────────────────
_item_cache: dict[str, dict] = {}
_cache_expiry: dict[str, datetime] = {}


async def _get_item_detail(client: httpx.AsyncClient, api_key: str, item_id: str) -> dict:
    now = datetime.now()
    if item_id in _item_cache and _cache_expiry.get(item_id, now) > now:
        return _item_cache[item_id]

    url = f"{NEOPLE_BASE}/items/{item_id}?apikey={api_key}"
    resp = await client.get(url)
    if resp.status_code == 200:
        data = resp.json()
        _item_cache[item_id] = data
        _cache_expiry[item_id] = now + timedelta(days=7)
        return data
    return {}


# ── Enchant parsing ───────────────────────────────────────────────────────────
def enchant_to_text(enchant: dict | None) -> str:
    """마법부여 JSON → 읽기 좋은 텍스트 (Apps Script enchantToText_ 이식)"""
    if not enchant:
        return ""

    lines: list[str] = []

    # explains 배열 (일반 설명)
    for line in enchant.get("explains", []):
        if line and not line.startswith("◇"):
            lines.append(line.strip())

    # status 배열 (수치)
    for stat in enchant.get("status", []):
        name = stat.get("name", "")
        value = stat.get("value", "")
        if name and value:
            lines.append(f"{name} +{value}")

    return " / ".join(lines) if lines else ""


def enchant_score(enchant: dict | None) -> int:
    """마법부여 점수 계산"""
    if not enchant:
        return 0
    total = 0
    for stat in enchant.get("status", []):
        name = stat.get("name", "")
        try:
            value = float(stat.get("value", 0))
        except (ValueError, TypeError):
            continue
        if name in ENCHANT_SCORE:
            info = ENCHANT_SCORE[name]
            total += int(value / info["per_value"]) * info["score"]
    return total


# ── Main fetch ────────────────────────────────────────────────────────────────
async def fetch_character(api_key: str, server: str, char_name: str) -> dict:
    server_code = SERVER_MAP.get(server, server.lower())

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. 캐릭터 검색
        search_resp = await client.get(
            f"{NEOPLE_BASE}/servers/{server_code}/characters",
            params={"characterName": char_name, "wordType": "full", "apikey": api_key},
        )
        if search_resp.status_code != 200:
            raise HTTPException(502, f"캐릭터 검색 실패: {search_resp.status_code}")

        rows = search_resp.json().get("rows", [])
        if not rows:
            raise HTTPException(404, "캐릭터를 찾을 수 없습니다")

        char = rows[0]
        char_id = char["characterId"]

        # 2. 병렬 요청
        urls = {
            "equip":    f"{NEOPLE_BASE}/servers/{server_code}/characters/{char_id}/equip/equipment?apikey={api_key}",
            "oath":     f"{NEOPLE_BASE}/servers/{server_code}/characters/{char_id}/equip/oath?apikey={api_key}",
            "avatar":   f"{NEOPLE_BASE}/servers/{server_code}/characters/{char_id}/equip/avatar?apikey={api_key}",
            "creature": f"{NEOPLE_BASE}/servers/{server_code}/characters/{char_id}/equip/creature?apikey={api_key}",
            "status":   f"{NEOPLE_BASE}/servers/{server_code}/characters/{char_id}/status?apikey={api_key}",
        }

        responses = await asyncio.gather(
            *[client.get(url) for url in urls.values()],
            return_exceptions=True,
        )

        def safe_json(resp, key=None):
            if isinstance(resp, Exception) or resp.status_code != 200:
                return {} if key else []
            data = resp.json()
            return data.get(key, data) if key else data

        equip_raw   = safe_json(responses[0], "equipment")
        oath_raw    = safe_json(responses[1])
        avatar_raw  = safe_json(responses[2], "avatar")
        creature_raw = safe_json(responses[3])
        status_raw  = safe_json(responses[4], "status")

        # 3. 장비 처리
        equipment = []
        for item in (equip_raw if isinstance(equip_raw, list) else []):
            enchant = item.get("enchant")
            equipment.append({
                "slot":          item.get("slotName", ""),
                "itemId":        item.get("itemId", ""),
                "itemName":      item.get("itemName", ""),
                "rarity":        item.get("itemRarity", ""),
                "reinforce":     item.get("reinforce", 0),
                "amplification": item.get("amplificationName", ""),
                "enchantText":   enchant_to_text(enchant),
                "enchantScore":  enchant_score(enchant),
                "enchantRaw":    enchant or {},
            })

        # 4. 아바타 처리
        avatar = []
        for av in (avatar_raw if isinstance(avatar_raw, list) else []):
            avatar.append({
                "slot":         av.get("slotName", ""),
                "itemName":     av.get("itemName", ""),
                "optionAbility": av.get("optionAbility", ""),
                "emblems": [
                    {"name": e.get("itemName", ""), "rarity": e.get("itemRarity", "")}
                    for e in av.get("emblems", [])
                ],
            })

        # 5. 스탯 처리
        status = {s["name"]: s["value"] for s in (status_raw if isinstance(status_raw, list) else [])}

        # 6. 세트 (oath)
        oath_sets = []
        for s in oath_raw.get("setItems", []):
            oath_sets.append({
                "setName":  s.get("setItemName", ""),
                "active":   s.get("activeSetNo", 0),
                "maxLevel": s.get("maxSetNo", 0),
                "items":    [i.get("itemName", "") for i in s.get("setItemList", [])],
            })

        # 7. 크리처
        creature = {}
        if isinstance(creature_raw, dict) and creature_raw.get("creature"):
            c = creature_raw["creature"]
            creature = {
                "itemName": c.get("itemName", ""),
                "itemRarity": c.get("itemRarity", ""),
                "artifact": [
                    {"slot": a.get("slotName", ""), "name": a.get("itemName", "")}
                    for a in c.get("artifact", [])
                ],
            }

        return {
            "characterId":   char_id,
            "characterName": char.get("characterName", ""),
            "server":        server,
            "jobName":       char.get("jobName", ""),
            "jobGrowName":   char.get("jobGrowName", ""),
            "level":         char.get("level", 0),
            "equipment":     equipment,
            "avatar":        avatar,
            "creature":      creature,
            "oath":          oath_sets,
            "status":        status,
            "enchantTotal":  sum(e["enchantScore"] for e in equipment),
        }


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/character")
async def get_character(
    server: str = Query(..., description="서버 (한국어 또는 영문 코드)"),
    name:   str = Query(..., description="캐릭터명"),
    apikey: Optional[str] = Query(None, description="Neople API Key (미입력 시 환경변수 사용)"),
):
    key = apikey or os.getenv("NEOPLE_API_KEY", "")
    if not key:
        raise HTTPException(400, "API 키가 필요합니다 (쿼리 파라미터 apikey 또는 환경변수 NEOPLE_API_KEY)")
    return await fetch_character(key, server, name)


@app.get("/servers")
async def get_servers():
    return {"servers": list(SERVER_MAP.keys())}


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}
