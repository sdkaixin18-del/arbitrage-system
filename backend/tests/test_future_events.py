from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.future_events import FutureEventDeleteInput, FutureEventInput, delete_future_event, event_to_out, upsert_future_event
from app.models import FutureEvent, FutureEventFeedback


def test_future_event_upsert_and_serialization() -> None:
    engine = create_engine("sqlite:///:memory:")
    FutureEvent.__table__.create(engine)

    with Session(engine) as db:
        payload = FutureEventInput(
            event_key="test-event",
            title="测试事件",
            category="财报",
            priority="S",
            stage="date_locked",
            start_date=date(2026, 7, 15),
            source_status="official",
            stock_mappings=[{"market": "A", "code": "000001", "name": "测试股", "role": "直接"}],
            validation_points=["核对正式公告"],
        )
        event, created = upsert_future_event(db, payload)
        db.commit()
        db.refresh(event)

        assert created is True
        assert event_to_out(event).stock_mappings[0].name == "测试股"

        updated_payload = payload.model_copy(update={"priority": "A", "price_expression": "已部分交易"})
        updated, created_again = upsert_future_event(db, updated_payload)
        db.commit()

        assert created_again is False
        assert updated.id == event.id
        assert db.scalar(select(FutureEvent)).priority == "A"


def test_future_event_feedback_snapshot_table() -> None:
    engine = create_engine("sqlite:///:memory:")
    FutureEventFeedback.__table__.create(engine)

    with Session(engine) as db:
        feedback = FutureEventFeedback(
            event_key="deleted-event",
            title="已删除事件",
            category="宏观数据",
            priority="A",
            stage="time_locked",
            action="manual_delete",
            reason_category="影响太弱",
            note="缺少明确A股承接",
            snapshot_json='{"title":"已删除事件"}',
        )
        db.add(feedback)
        db.commit()

        saved = db.scalar(select(FutureEventFeedback))
        assert saved is not None
        assert saved.event_key == "deleted-event"
        assert saved.reason_category == "影响太弱"
        assert saved.note == "缺少明确A股承接"


def test_delete_future_event_saves_optional_reason_and_snapshot() -> None:
    engine = create_engine("sqlite:///:memory:")
    FutureEvent.__table__.create(engine)
    FutureEventFeedback.__table__.create(engine)

    with Session(engine) as db:
        event, _ = upsert_future_event(
            db,
            FutureEventInput(
                event_key="event-to-delete",
                title="待删除事件",
                category="航天",
                priority="S",
                stage="date_locked",
                start_date=date(2026, 7, 20),
                source_status="official",
                source_urls=["https://example.com/official"],
            ),
        )
        db.commit()
        event_id = event.id

        result = delete_future_event(
            event_id,
            FutureEventDeleteInput(reason_category="时间不明确", reason="官方仅给出日期窗口"),
            db,
        )

        assert result["status"] == "ok"
        assert db.get(FutureEvent, event_id) is None
        saved = db.scalar(select(FutureEventFeedback))
        assert saved is not None
        assert saved.reason_category == "时间不明确"
        assert saved.note == "官方仅给出日期窗口"
        assert '"title":"待删除事件"' in saved.snapshot_json

        repeated = delete_future_event(
            event_id,
            FutureEventDeleteInput(reason_category="其他", reason="重复请求"),
            db,
        )
        assert repeated["status"] == "ok"
        assert repeated["message"] == "未来事件已不存在"
        assert len(list(db.scalars(select(FutureEventFeedback)))) == 1
