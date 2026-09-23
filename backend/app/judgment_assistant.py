from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.database import get_data_root

DEFAULT_LIBRARY_RELATIVE_PATH = Path("knowledge") / "投资文章学习库.md"
MOTHER_CARD_HEADING_RE = re.compile(r"^##\s+(?P<id>M\d{2})[｜|](?P<title>.+?)\s*$", re.MULTILINE)
CARD_HEADING_RE = re.compile(r"^##\s+(?P<id>\d{3})[｜|](?P<title>.+?)\s*$", re.MULTILINE)
SECOND_LEVEL_HEADING_RE = re.compile(r"^##\s+", re.MULTILINE)
SECTION_HEADING_RE = re.compile(r"^###\s+(?P<title>.+?)\s*$", re.MULTILINE)
RECORD_HEADING_RE = re.compile(r"^###\s+(?P<title>.+?)\s*$", re.MULTILINE)
RECORD_SECTION_HEADING = "## 判断使用记录"
RECORD_TEMPLATE_HEADING = "### 使用记录模板"
CONCLUSIONS = {"通过", "观察", "降级", "放弃"}


def knowledge_library_path() -> Path:
    configured = os.environ.get("INVESTMENT_KNOWLEDGE_LIBRARY_PATH")
    return Path(configured).expanduser() if configured else get_data_root() / DEFAULT_LIBRARY_RELATIVE_PATH


def judgment_assistant_overview() -> dict[str, Any]:
    path = knowledge_library_path()
    if not path.exists():
        return {
            "status": "not_found",
            "message": f"没有找到知识库文件：{path}",
            "library_path": str(path),
            "updated_at": None,
            "mother_cards": [],
            "cards": [],
            "card_relations": [],
            "recent_records": [],
        }

    text = path.read_text(encoding="utf-8")
    mother_cards = parse_mother_cards(text)
    cards = parse_cards(text)
    return {
        "status": "ok",
        "message": "判断辅助知识库已加载。",
        "library_path": str(path),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone(),
        "mother_cards": mother_cards,
        "cards": cards,
        "card_relations": build_card_relations(mother_cards, cards),
        "recent_records": parse_records(text),
    }


def append_judgment_record(payload: dict[str, Any]) -> dict[str, Any]:
    conclusion = str(payload.get("conclusion") or "").strip()
    if conclusion not in CONCLUSIONS:
        raise ValueError("当前结论必须是：通过 / 观察 / 降级 / 放弃")

    judgment_object = str(payload.get("judgment_object") or "").strip()
    if not judgment_object:
        raise ValueError("判断对象不能为空")

    path = knowledge_library_path()
    text = _read_or_create_library(path)
    mother_cards = parse_mother_cards(text)
    cards = parse_cards(text)
    card_titles = {
        **{card["id"]: f'{card["id"]}｜{card["title"]}' for card in mother_cards},
        **{card["id"]: f'{card["id"]}｜{card["title"]}' for card in cards},
    }
    called_cards = [
        card_titles.get(str(card_id).strip(), str(card_id).strip())
        for card_id in payload.get("called_cards", [])
        if str(card_id).strip()
    ]
    now = datetime.now().astimezone()
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    block = "\n".join(
        [
            "",
            f"### {timestamp}｜{_one_line(judgment_object)}",
            "",
            f"- 日期：{timestamp}",
            f"- 判断对象：{_one_line(judgment_object)}",
            f"- 调用卡片：{_one_line('；'.join(called_cards) if called_cards else '-')}",
            f"- 满足条件：{_one_line(payload.get('satisfied'))}",
            f"- 不满足条件：{_one_line(payload.get('unsatisfied'))}",
            f"- 当前结论：{conclusion}",
            f"- 后续验证点：{_one_line(payload.get('next_validation'))}",
            f"- 备注：{_one_line(payload.get('note'))}",
        ]
    )
    text = _ensure_record_section(text)
    path.write_text(text.rstrip() + "\n" + block + "\n", encoding="utf-8")
    records = parse_records(path.read_text(encoding="utf-8"))
    return records[0] if records else {}


def parse_mother_cards(text: str) -> list[dict[str, Any]]:
    mother_cards: list[dict[str, Any]] = []
    for match in MOTHER_CARD_HEADING_RE.finditer(text):
        body_start = match.end()
        next_heading = SECOND_LEVEL_HEADING_RE.search(text, body_start)
        body = text[body_start : next_heading.start() if next_heading else len(text)]
        sections = _parse_sections(body)
        mother_cards.append(
            {
                "id": match.group("id"),
                "title": match.group("title").strip(),
                "problem": _compact_text(sections.get("解决的问题")),
                "call_conditions": _extract_list_items(sections.get("调用条件")),
                "checklist": _extract_list_items(sections.get("可执行判断清单")),
                "supporting_card_ids": _extract_card_ids(sections.get("支持卡片"), r"\b\d{3}\b"),
                "boundaries": _extract_list_items(sections.get("适用边界")),
                "revision_notes": _extract_list_items(sections.get("修正记录")),
                "reuse_hint": _compact_text(sections.get("后续复用提示")),
            }
        )
    return mother_cards


def parse_cards(text: str) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for match in CARD_HEADING_RE.finditer(text):
        body_start = match.end()
        next_heading = SECOND_LEVEL_HEADING_RE.search(text, body_start)
        body = text[body_start : next_heading.start() if next_heading else len(text)]
        sections = _parse_sections(body)
        cards.append(
            {
                "id": match.group("id"),
                "title": match.group("title").strip(),
                "core": _compact_text(sections.get("一句话核心")),
                "mother_card_ids": _extract_card_ids(sections.get("所属母卡"), r"\bM\d{2}\b"),
                "checklist": _extract_list_items(sections.get("可执行判断清单")),
                "boundaries": _extract_list_items(sections.get("适用边界")),
                "revision_notes": _extract_list_items(sections.get("修正记录")),
                "reuse_hint": _compact_text(sections.get("后续复用提示")),
            }
        )
    return cards


def build_card_relations(mother_cards: list[dict[str, Any]], cards: list[dict[str, Any]]) -> list[dict[str, str]]:
    relations: set[tuple[str, str]] = set()
    mother_ids = {card["id"] for card in mother_cards}
    card_ids = {card["id"] for card in cards}

    for mother_card in mother_cards:
        for card_id in mother_card.get("supporting_card_ids", []):
            if card_id in card_ids:
                relations.add((mother_card["id"], card_id))

    for card in cards:
        for mother_card_id in card.get("mother_card_ids", []):
            if mother_card_id in mother_ids:
                relations.add((mother_card_id, card["id"]))

    return [{"mother_card_id": mother_id, "card_id": card_id} for mother_id, card_id in sorted(relations)]


def parse_records(text: str) -> list[dict[str, Any]]:
    section_start = text.find(RECORD_SECTION_HEADING)
    if section_start < 0:
        return []

    section = text[section_start + len(RECORD_SECTION_HEADING) :]
    records: list[dict[str, Any]] = []
    headings = list(RECORD_HEADING_RE.finditer(section))
    for index, match in enumerate(headings):
        title = match.group("title").strip()
        if title == "使用记录模板":
            continue

        block_start = match.end()
        block_end = headings[index + 1].start() if index + 1 < len(headings) else len(section)
        fields = _parse_record_fields(section[block_start:block_end])
        records.append(
            {
                "id": f"record-{index}",
                "title": title,
                "date": fields.get("日期") or "",
                "judgment_object": fields.get("判断对象") or title,
                "called_cards": _split_list_field(fields.get("调用卡片")),
                "satisfied": fields.get("满足条件") or "",
                "unsatisfied": fields.get("不满足条件") or "",
                "conclusion": fields.get("当前结论") or "",
                "next_validation": fields.get("后续验证点") or "",
                "note": fields.get("备注") or "",
            }
        )
    return list(reversed(records))[:20]


def _parse_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    headings = list(SECTION_HEADING_RE.finditer(body))
    for index, match in enumerate(headings):
        title = match.group("title").strip()
        section_start = match.end()
        section_end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections[title] = body[section_start:section_end].strip()
    return sections


def _extract_list_items(value: str | None) -> list[str]:
    if not value:
        return []
    items = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif re.match(r"^\d+\.\s+", line):
            items.append(re.sub(r"^\d+\.\s+", "", line).strip())
    return [item for item in items if item]


def _extract_card_ids(value: str | None, pattern: str) -> list[str]:
    if not value:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in _extract_list_items(value) or value.splitlines():
        for match in re.findall(pattern, item):
            if match not in seen:
                seen.add(match)
                result.append(match)
    return result


def _compact_text(value: str | None) -> str:
    if not value:
        return ""
    lines = [line.strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _parse_record_fields(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        content = line[2:]
        if "：" in content:
            key, value = content.split("：", 1)
        elif ":" in content:
            key, value = content.split(":", 1)
        else:
            continue
        fields[key.strip()] = value.strip()
    return fields


def _split_list_field(value: str | None) -> list[str]:
    if not value or value.strip() == "-":
        return []
    parts = re.split(r"[；;]", value)
    return [part.strip() for part in parts if part.strip()]


def _read_or_create_library(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "# 投资文章学习库\n\n## 判断使用记录\n\n### 使用记录模板\n\n"
    path.write_text(text, encoding="utf-8")
    return text


def _ensure_record_section(text: str) -> str:
    if RECORD_SECTION_HEADING not in text:
        return text.rstrip() + f"\n\n---\n\n{RECORD_SECTION_HEADING}\n\n{RECORD_TEMPLATE_HEADING}\n\n"
    if RECORD_TEMPLATE_HEADING not in text[text.find(RECORD_SECTION_HEADING) :]:
        return text.rstrip() + f"\n\n{RECORD_TEMPLATE_HEADING}\n\n"
    return text


def _one_line(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    return re.sub(r"\s+", " ", text)
