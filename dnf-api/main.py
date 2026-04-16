"""
DNF 캐릭터 시트 API
Neople Open API를 활용해 장비/아바타/크리처/스탯 정보를 조회합니다.
"""

import asyncio
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="DNF Character Sheet API", version="2.0.0")

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

NEOPLE_BASE = "https://api.neople.co.kr/df"

SERVER_MAP: dict[str, str] = {
    "카인": "cain", "디레지에": "diregie", "시로코": "siroco",
    "바칼": "bakal", "하자드": "hilder", "안톤": "anton", "카시야스": "kasias",
}

# 마법부여 등급 키워드 (설명 텍스트에서 파싱)
ENCHANT_GRADES = ["환상급", "최상급", "영상급", "상급", "중급", "하급", "특수"]

# 아이템 상세 캐시 (7일)
_item_cache: dict[str, dict] = {}
_cache_expiry: dict[str, datetime] = {}


# ── Enchant helpers ────────────────────────────────────────────────────────────
def parse_enchant_grade(enchant: dict | None) -> str:
    """마법부여 등급 파싱 (환상급/최상급/영상급/상급/중급/하급)"""
    if not enchant:
        return ""
    for line in enchant.get("explains", []):
        for grade in ENCHANT_GRADES:
            if grade in (line or ""):
                return grade
    return ""


def enchant_to_text(enchant: dict | None) -> str:
    """마법부여 JSON → 읽기 좋은 텍스트"""
    if not enchant:
        return ""
    lines: list[str] = []
    for line in enchant.get("explains", []):
        s = (line or "").strip()
        if s:
            lines.append(s)
    for stat in enchant.get("status", []):
        name  = stat.get("name", "")
        value = stat.get("value", "")
        if name and value is not None and value != "":
            lines.append(f"{name} +{value}")
    return "\n".join(lines) if lines else ""


# ── Calibration helper ─────────────────────────────────────────────────────────
def parse_calibration(item: dict) -> list[dict]:
    """조율 옵션 파싱 (baekOption / calibrationInfo)"""
    raw = item.get("baekOption") or item.get("calibrationInfo") or []
    if isinstance(raw, list):
        return [
            {"name": o.get("name", ""), "value": o.get("value", "")}
            for o in raw if o.get("name")
        ]
    return []


# ── Main fetch ─────────────────────────────────────────────────────────────────
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

        char    = rows[0]
        char_id = char["characterId"]

        # 2. 병렬 조회
        urls = [
            f"{NEOPLE_BASE}/servers/{server_code}/characters/{char_id}/equip/equipment?apikey={api_key}",
            f"{NEOPLE_BASE}/servers/{server_code}/characters/{char_id}/equip/oath?apikey={api_key}",
            f"{NEOPLE_BASE}/servers/{server_code}/characters/{char_id}/equip/avatar?apikey={api_key}",
            f"{NEOPLE_BASE}/servers/{server_code}/characters/{char_id}/equip/creature?apikey={api_key}",
            f"{NEOPLE_BASE}/servers/{server_code}/characters/{char_id}/status?apikey={api_key}",
        ]
        responses = await asyncio.gather(*[client.get(u) for u in urls], return_exceptions=True)

        def safe(resp, key=None):
            if isinstance(resp, Exception) or resp.status_code != 200:
                return {} if key is None else []
            data = resp.json()
            if key is None:
                return data
            return data.get(key, [])

        equip_raw    = safe(responses[0], "equipment")
        oath_raw     = safe(responses[1])
        avatar_raw   = safe(responses[2], "avatar")
        creature_raw = safe(responses[3])
        status_raw   = safe(responses[4], "status")

        # 3. 장비 처리
        equipment = []
        for item in (equip_raw if isinstance(equip_raw, list) else []):
            enchant = item.get("enchant") or {}
            equipment.append({
                "slot":          item.get("slotName", ""),
                "itemName":      item.get("itemName", ""),
                "rarity":        item.get("itemRarity", ""),
                "reinforce":     item.get("reinforce", 0),
                "amplification": item.get("amplificationName", ""),
                "enchantText":   enchant_to_text(enchant),
                "enchantGrade":  parse_enchant_grade(enchant),
                "enchantStatus": enchant.get("status", []),   # 상세 스탯
                "calibration":   parse_calibration(item),     # 조율
            })

        # 4. 아바타 처리 (칭호/오라/무기아바타 포함)
        avatar = []
        for av in (avatar_raw if isinstance(avatar_raw, list) else []):
            avatar.append({
                "slot":          av.get("slotName", ""),
                "itemName":      av.get("itemName", ""),
                "rarity":        av.get("itemRarity", ""),
                "optionAbility": av.get("optionAbility", ""),
                "emblems": [
                    {"name": e.get("itemName", ""), "rarity": e.get("itemRarity", "")}
                    for e in av.get("emblems", [])
                ],
            })

        # 5. 스탯
        status = {s["name"]: s["value"] for s in (status_raw if isinstance(status_raw, list) else [])}

        # 6. 세트 — 서약세트 / 장비세트 구분
        oath_sets  = []   # 서약 세트
        equip_sets = []   # 장비 세트

        for s in oath_raw.get("oathSetItems", []):
            oath_sets.append({
                "setName":    s.get("setItemName", ""),
                "active":     s.get("activeSetNo", 0),
                "maxLevel":   s.get("maxSetNo", 0),
                "oathPoint":  s.get("oathPoint", 0),          # 서약 포인트
                "items":      [i.get("itemName", "") for i in s.get("setItemList", [])],
            })

        for s in oath_raw.get("setItems", []):
            # oathSetItems와 중복 방지
            is_oath = any(o["setName"] == s.get("setItemName", "") for o in oath_sets)
            if not is_oath:
                equip_sets.append({
                    "setName":  s.get("setItemName", ""),
                    "active":   s.get("activeSetNo", 0),
                    "maxLevel": s.get("maxSetNo", 0),
                    "items":    [i.get("itemName", "") for i in s.get("setItemList", [])],
                })

        # oathSetItems 없으면 setItems 전체를 장비세트로
        if not oath_sets and not equip_sets:
            for s in oath_raw.get("setItems", []):
                equip_sets.append({
                    "setName":  s.get("setItemName", ""),
                    "active":   s.get("activeSetNo", 0),
                    "maxLevel": s.get("maxSetNo", 0),
                    "items":    [i.get("itemName", "") for i in s.get("setItemList", [])],
                })

        # 7. 크리처 + 아티팩트 (레드/블루/그린)
        creature = {}
        if isinstance(creature_raw, dict) and creature_raw.get("creature"):
            c = creature_raw["creature"]
            artifact_slot_map = {"RED": "레드", "BLUE": "블루", "GREEN": "그린"}
            creature = {
                "itemName":   c.get("itemName", ""),
                "itemRarity": c.get("itemRarity", ""),
                "artifact": [
                    {
                        "slot": artifact_slot_map.get(
                            (a.get("slotInfo") or a.get("slotId") or "").upper(),
                            a.get("slotInfo") or a.get("slotName") or "",
                        ),
                        "name":   a.get("itemName", ""),
                        "rarity": a.get("itemRarity", ""),
                    }
                    for a in c.get("artifact", [])
                ],
            }

        return {
            "characterId":   char["characterId"],
            "characterName": char.get("characterName", ""),
            "server":        server,
            "jobName":       char.get("jobName", ""),
            "jobGrowName":   char.get("jobGrowName", ""),
            "level":         char.get("level", 0),
            "equipment":     equipment,
            "avatar":        avatar,
            "creature":      creature,
            "oathSets":      oath_sets,
            "equipSets":     equip_sets,
            "status":        status,
        }


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/character")
async def get_character(
    server: str = Query(...),
    name:   str = Query(...),
    apikey: Optional[str] = Query(None),
):
    key = apikey or os.getenv("NEOPLE_API_KEY", "")
    if not key:
        raise HTTPException(400, "API 키가 필요합니다")
    return await fetch_character(key, server, name)


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}
