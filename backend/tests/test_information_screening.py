from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.information_screening import (
    PromotionInput,
    ScreeningBatchInput,
    ScreeningDeleteInput,
    ScreeningItemInput,
    delete_item,
    promote_item,
    upsert_batch,
)
from app.models import (
    AStock,
    IndustryChain,
    IndustryChainEvidence,
    IndustryChainTask,
    IndustryTrendUpdate,
    InformationScreeningFeedback,
    InformationScreeningItem,
)
from app.watchlist_announcements import lookup_cninfo_announcements


def test_information_screening_batch_and_promotion(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'information-screening.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        chain = IndustryChain(name="高速互联", phase="验证期")
        db.add(chain)
        db.commit()
        db.refresh(chain)

        result = upsert_batch(
            ScreeningBatchInput(
                batch_key="2026-07-14-am",
                title="7月14日早盘信息筛选",
                window_start=datetime(2026, 7, 14, 8, tzinfo=timezone.utc),
                window_end=datetime(2026, 7, 14, 17, tzinfo=timezone.utc),
                source_scope=["知识星球", "全球新闻"],
                read_count=30,
                deduplicated_count=20,
                items=[
                    ScreeningItemInput(
                        item_key="verified-order",
                        title="订单获得公告确认",
                        bucket="verified",
                        is_top=True,
                        importance=5,
                        marginal_change="订单规模首次被正式披露",
                        verification_status="verified",
                        evidence_summary="公司公告确认",
                        validation_points=["跟踪收入确认"],
                    ),
                    ScreeningItemInput(
                        item_key="eye-catching",
                        title="大客户可能开始放量",
                        bucket="eye_catching",
                        importance=4,
                        marginal_change="星球首次给出明确交付时点",
                        verification_status="unverified",
                        validation_points=["核对公司公告", "核对产业链订单"],
                    ),
                    ScreeningItemInput(
                        item_key="old-story",
                        title="行业空间重复转发",
                        bucket="filtered",
                        filter_reason="旧逻辑，没有公司层面新增量",
                        recovery_condition="出现订单或业绩上调",
                    ),
                ],
            ),
            db,
        )

        assert result["created"] is True
        assert result["batch"]["stats"] == {
            "read": 30,
            "deduplicated": 20,
            "top": 1,
            "verified": 1,
            "eye_catching": 1,
            "filtered": 1,
            "official_required": 0,
            "official_matched": 0,
            "official_unresolved": 0,
        }

        by_key = {item["item_key"]: item for item in result["items"]}
        verified = promote_item(by_key["verified-order"]["id"], PromotionInput(chain_id=chain.id), db)
        eye_catching = promote_item(by_key["eye-catching"]["id"], PromotionInput(chain_id=chain.id), db)

        assert verified["promotion_type"] == "evidence_and_update"
        assert eye_catching["promotion_type"] == "validation_task"
        assert db.scalar(select(IndustryChainEvidence)).title == "订单获得公告确认"
        assert db.scalar(select(IndustryTrendUpdate)).content == "订单规模首次被正式披露"
        assert db.scalar(select(IndustryChainTask)).status == "验证中"

        deleted = delete_item(
            by_key["eye-catching"]["id"],
            ScreeningDeleteInput(reason_category="缺少硬证据", note="必须等公告确认客户和交付时点"),
            db,
        )
        assert deleted["feedback_rule"] == "缺少硬证据"
        feedback = db.scalar(select(InformationScreeningFeedback))
        assert feedback is not None
        assert feedback.item_key == "eye-catching"
        assert feedback.reason_category == "缺少硬证据"
        assert "大客户可能开始放量" in feedback.snapshot_json

        regenerated = upsert_batch(
            ScreeningBatchInput(
                batch_key="2026-07-14-am",
                title="7月14日早盘信息筛选",
                items=[
                    ScreeningItemInput(
                        item_key="eye-catching",
                        title="大客户可能开始放量",
                        bucket="eye_catching",
                        marginal_change="重复生成",
                    )
                ],
            ),
            db,
        )
        assert regenerated["suppressed_items"] == 1
        assert db.scalar(select(InformationScreeningItem).where(InformationScreeningItem.item_key == "eye-catching")) is None


def test_official_guard_blocks_false_negative_and_records_verified_match(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'official-guard.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    def matched_lookup(_db, code, _start_date, _end_date):
        assert code == "920125"
        return {
            "status": "matched",
            "stock_code": code,
            "message": "命中1条",
            "matches": [
                {
                    "title": "2026年半年度业绩预告公告",
                    "source_name": "官方公告",
                    "source_url": "https://static.cninfo.com.cn/finalpage/2026-07-14/1225424336.PDF",
                    "published_at": datetime(2026, 7, 14, tzinfo=timezone.utc),
                }
            ],
        }

    unverified = ScreeningBatchInput(
        batch_key="official-guard",
        title="公告核验防线",
        window_start=datetime(2026, 7, 14, tzinfo=timezone.utc),
        window_end=datetime(2026, 7, 15, tzinfo=timezone.utc),
        items=[
            ScreeningItemInput(
                item_key="hongshida",
                title="鸿仕达被传上半年净利润最高增369%，公告待补",
                summary="雪球称净利润同比增长369%",
                source_type="xueqiu",
                related_stocks=["鸿仕达 920125"],
                bucket="eye_catching",
                marginal_change="利润增速抢眼",
                verification_status="unverified",
            )
        ],
    )
    with Session(engine) as db, pytest.raises(HTTPException) as error:
        upsert_batch(unverified, db, official_lookup=matched_lookup)
    assert error.value.status_code == 409
    assert "禁止继续写成" in str(error.value.detail)

    verified = unverified.model_copy(
        update={
            "items": [
                ScreeningItemInput(
                    item_key="hongshida",
                    title="鸿仕达半年报预告：净利润2200万—2600万元",
                    summary="公司预计上半年归母净利润同比增长296.75%—368.88%",
                    source_type="announcement",
                    source_name="巨潮资讯",
                    related_stocks=["鸿仕达 920125"],
                    bucket="verified",
                    marginal_change="官方公告确认利润高增",
                    verification_status="verified",
                    evidence_summary="公告编号2026-091",
                )
            ]
        }
    )
    with Session(engine) as db:
        result = upsert_batch(verified, db, official_lookup=matched_lookup)
        item = result["items"][0]
        assert item["official_check_status"] == "matched"
        assert item["official_source_url"].endswith("1225424336.PDF")
        assert result["batch"]["stats"]["official_matched"] == 1
        assert result["batch"]["stats"]["official_unresolved"] == 0


def test_direct_cninfo_lookup_does_not_require_watchlist(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'direct-cninfo.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    requested: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "announcements": [
                    {
                        "announcementId": "1225424336",
                        "announcementTitle": "2026年半年度业绩预告公告",
                        "announcementTime": 1783958400000,
                        "adjunctUrl": "finalpage/2026-07-14/1225424336.PDF",
                    }
                ]
            }

    class FakeClient:
        def post(self, _url, data, headers):
            requested.update({"data": data, "headers": headers})
            return FakeResponse()

    class FakeContext:
        def __enter__(self):
            return FakeClient()

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

    monkeypatch.setattr("app.watchlist_announcements.http_client", lambda: FakeContext())

    with Session(engine) as db:
        db.add(AStock(code="920125", name="鸿仕达", exchange="BJ", full_code="BJ920125", org_id="gfbj0874538", pinyin="hongshida", source="cninfo"))
        db.commit()
        result = lookup_cninfo_announcements(db, "920125", date(2026, 7, 14), date(2026, 7, 14))

    assert result["status"] == "matched"
    assert result["matches"][0]["source_url"].endswith("1225424336.PDF")
    assert requested["data"]["column"] == "bj"
    assert requested["data"]["seDate"] == "2026-07-14~2026-07-14"


def test_direct_sse_lookup_uses_exchange_official_source(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'direct-sse.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    requested: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "pageHelp": {
                    "data": [
                        {
                            "SECURITY_CODE": "603296",
                            "SSEDATE": "2026-07-14",
                            "TITLE": "华勤技术2026年半年度业绩预增公告",
                            "URL": "/disclosure/listedinfo/announcement/c/new/2026-07-14/603296_20260714_LX6Y.pdf",
                        }
                    ]
                }
            }

    class FakeClient:
        def get(self, _url, params, headers):
            requested.update({"params": params, "headers": headers})
            return FakeResponse()

    class FakeContext:
        def __enter__(self):
            return FakeClient()

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

    monkeypatch.setattr("app.watchlist_announcements.http_client", lambda: FakeContext())

    with Session(engine) as db:
        db.add(AStock(code="603296", name="华勤技术", exchange="SH", full_code="SH603296", pinyin="huaqinjishu", source="akshare"))
        db.commit()
        result = lookup_cninfo_announcements(db, "603296", date(2026, 7, 14), date(2026, 7, 15))

    assert result["status"] == "matched"
    assert result["matches"][0]["source_url"].startswith("https://www.sse.com.cn/")
    assert requested["params"]["productId"] == "603296"
    assert requested["params"]["beginDate"] == "2026-07-14"


def test_official_guard_expands_overnight_disclosure_window(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'overnight-window.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    windows: list[tuple[date, date]] = []

    def no_match_lookup(_db, _code, start_date, end_date):
        windows.append((start_date, end_date))
        return {"status": "not_found", "message": "未命中", "matches": []}

    with Session(engine) as db:
        upsert_batch(
            ScreeningBatchInput(
                batch_key="overnight-window",
                title="隔夜公告日期边界",
                items=[
                    ScreeningItemInput(
                        item_key="late-night",
                        title="某公司发布业绩预告",
                        summary="净利润同比增长",
                        published_at=datetime(2026, 7, 14, 23, 58, tzinfo=timezone(timedelta(hours=8))),
                        related_stocks=["某公司 603296"],
                        bucket="eye_catching",
                        marginal_change="晚间线索",
                        verification_status="partial",
                    )
                ],
            ),
            db,
            official_lookup=no_match_lookup,
        )

    assert windows == [(date(2026, 7, 14), date(2026, 7, 15))]
