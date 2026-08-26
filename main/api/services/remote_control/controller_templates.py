"""Per-user CRUD and built-in overlay logic for controller templates."""

from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from sqlmodel import Session, select

from api.models import RemoteControllerTemplate, User
from .controller_schema import (
    MAX_TEMPLATES_PER_USER,
    SCHEMA_NAME,
    BrowserAction,
    ControllerControl,
    ControllerLayout,
    EmitAction,
    KeyAction,
    TemplateContent,
    TemplateCreate,
    TemplateDocument,
    TemplateUpdate,
)


class TemplateNotFoundError(LookupError):
    pass


class TemplateConflictError(RuntimeError):
    pass


class TemplateLimitError(RuntimeError):
    pass


def _button(control_id: str, label: str, action, tone: str = "default") -> ControllerControl:
    return ControllerControl(id=control_id, kind="button", label=label, tone=tone, action=action)


def _slider(control_id: str, label: str, event: str) -> ControllerControl:
    return ControllerControl.model_validate({
        "id": control_id,
        "kind": "slider",
        "label": label,
        "action": EmitAction(type="emit", event=event),
        "min": 500,
        "max": 2500,
        "step": 1,
    })


def _keys(*items: tuple[str, str, str]) -> list[ControllerControl]:
    return [_button(control_id, label, KeyAction(type="key", key=key)) for control_id, label, key in items]


def _builtin_templates() -> dict[str, TemplateCreate]:
    common = {
        "schema": SCHEMA_NAME,
        "requiredCapabilities": ["remote_control"],
        "layout": ControllerLayout(columns=3, gap="sm"),
    }
    builtins = [
        TemplateCreate(
            **common,
            id="direction",
            name="方向遥控器",
            deviceTypes=["desktop", "android", "browser"],
            controls=_keys(
                ("up", "上", "ArrowUp"), ("left", "左", "ArrowLeft"),
                ("ok", "确定", "Enter"), ("right", "右", "ArrowRight"),
                ("down", "下", "ArrowDown"), ("back", "返回", "Escape"),
            ),
        ),
        TemplateCreate(
            **common,
            id="media",
            name="媒体遥控器",
            deviceTypes=["desktop", "android"],
            controls=_keys(
                ("previous", "上一首", "MediaTrackPrevious"),
                ("play-pause", "播放/暂停", "MediaPlayPause"),
                ("next", "下一首", "MediaTrackNext"),
                ("volume-down", "音量-", "AudioVolumeDown"),
                ("mute", "静音", "AudioVolumeMute"),
                ("volume-up", "音量+", "AudioVolumeUp"),
            ),
        ),
        TemplateCreate(
            **common,
            id="presentation",
            name="演示遥控器",
            deviceTypes=["desktop"],
            controls=_keys(
                ("previous", "上一页", "PageUp"), ("next", "下一页", "PageDown"),
                ("start", "开始", "F5"), ("exit", "退出", "Escape"),
            ),
        ),
        TemplateCreate(
            **common,
            id="browser",
            name="浏览器遥控器",
            deviceTypes=["browser"],
            controls=[
                _button("back", "后退", BrowserAction(type="browser", action="back")),
                _button("reload", "刷新", BrowserAction(type="browser", action="reload"), "primary"),
                _button("forward", "前进", BrowserAction(type="browser", action="forward")),
            ],
        ),
        TemplateCreate(
            schema=SCHEMA_NAME,
            id="jibotarm",
            name="AI Mechanical Arm",
            deviceTypes=["custom"],
            requiredCapabilities=["remote_control", "remote_controller_templates"],
            layout=ControllerLayout(columns=2, gap="md"),
            controls=[
                _slider(f"joint{joint}", f"关节 {joint}", f"jibotarm.joint{joint}.position_p")
                for joint in range(1, 7)
            ],
        ),
    ]
    return {item.id: item for item in builtins}


BUILTIN_TEMPLATES = _builtin_templates()
BUILTIN_REVISION = 1


def _content_payload(content: TemplateContent) -> dict:
    payload = content.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
        exclude={"id", "revision", "builtin", "expected_revision"},
    )
    return TemplateContent.model_validate(payload).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


def _serialize(content: TemplateContent) -> str:
    return json.dumps(_content_payload(content), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _document(content: TemplateContent, template_id: str, revision: int, builtin: bool) -> TemplateDocument:
    return TemplateDocument(
        **_content_payload(content),
        id=template_id,
        revision=revision,
        builtin=builtin,
    )


def _builtin_document(template_id: str) -> Optional[TemplateDocument]:
    template = BUILTIN_TEMPLATES.get(template_id)
    return _document(template, template_id, BUILTIN_REVISION, True) if template else None


def _row_content(row: RemoteControllerTemplate) -> TemplateContent:
    return TemplateContent.model_validate_json(row.document_json)


def row_document(row: RemoteControllerTemplate) -> TemplateDocument:
    return _document(_row_content(row), row.template_id, row.revision, row.builtin_override)


def template_etag(document: TemplateDocument) -> str:
    return f'W/"rct-{document.id}-{document.revision}"'


def _user_rows(session: Session, user_id: int, *, include_deleted: bool = False):
    statement = select(RemoteControllerTemplate).where(RemoteControllerTemplate.user_id == user_id)
    if not include_deleted:
        statement = statement.where(RemoteControllerTemplate.deleted_at.is_(None))
    return session.exec(statement.order_by(RemoteControllerTemplate.template_id)).all()


def _owned_row(session: Session, user_id: int, template_id: str, *, include_deleted: bool = False):
    statement = select(RemoteControllerTemplate).where(
        RemoteControllerTemplate.user_id == user_id,
        RemoteControllerTemplate.template_id == template_id,
    )
    if not include_deleted:
        statement = statement.where(RemoteControllerTemplate.deleted_at.is_(None))
    return session.exec(statement).first()


def _lock_user(session: Session, user_id: int) -> None:
    row = session.exec(select(User).where(User.id == user_id).with_for_update()).first()
    if row is None:
        raise TemplateNotFoundError("user not found")


def list_templates(
    session: Session,
    user_id: int,
    *,
    device_type: Optional[str] = None,
    capability: Optional[str] = None,
) -> list[TemplateDocument]:
    documents = {key: _builtin_document(key) for key in BUILTIN_TEMPLATES}
    for row in _user_rows(session, user_id):
        documents[row.template_id] = row_document(row)
    result = [item for item in documents.values() if item is not None]
    if device_type:
        result = [item for item in result if device_type in item.device_types]
    if capability:
        result = [item for item in result if capability in item.required_capabilities]
    return sorted(result, key=lambda item: (not item.builtin, item.id))


def get_template(session: Session, user_id: int, template_id: str) -> TemplateDocument:
    row = _owned_row(session, user_id, template_id)
    if row:
        return row_document(row)
    builtin = _builtin_document(template_id)
    if builtin:
        return builtin
    raise TemplateNotFoundError("template not found")


def create_template(session: Session, user_id: int, body: TemplateCreate) -> TemplateDocument:
    _lock_user(session, user_id)
    if body.id in BUILTIN_TEMPLATES:
        raise TemplateConflictError("built-in template id cannot be created")
    active_custom = [row for row in _user_rows(session, user_id) if not row.builtin_override]
    existing = _owned_row(session, user_id, body.id, include_deleted=True)
    if existing and existing.deleted_at is None:
        raise TemplateConflictError("template id already exists")
    if (existing is None or existing.deleted_at is not None) and len(active_custom) >= MAX_TEMPLATES_PER_USER:
        raise TemplateLimitError("custom template limit reached")
    now = time.time()
    if existing:
        existing.document_json = _serialize(body)
        existing.revision += 1
        existing.deleted_at = None
        existing.updated_at = now
        row = existing
    else:
        row = RemoteControllerTemplate(
            id=f"rct_{uuid.uuid4().hex}",
            user_id=user_id,
            template_id=body.id,
            document_json=_serialize(body),
            revision=1,
            builtin_override=False,
            created_at=now,
            updated_at=now,
        )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row_document(row)


def update_template(
    session: Session,
    user_id: int,
    template_id: str,
    body: TemplateUpdate,
) -> TemplateDocument:
    _lock_user(session, user_id)
    row = _owned_row(session, user_id, template_id)
    current_revision = row.revision if row else BUILTIN_REVISION if template_id in BUILTIN_TEMPLATES else None
    if current_revision is None:
        raise TemplateNotFoundError("template not found")
    if body.expected_revision != current_revision:
        raise TemplateConflictError("template revision conflict")
    next_revision = current_revision + 1
    now = time.time()
    if row is None:
        row = RemoteControllerTemplate(
            id=f"rct_{uuid.uuid4().hex}",
            user_id=user_id,
            template_id=template_id,
            document_json=_serialize(body),
            revision=next_revision,
            builtin_override=True,
            created_at=now,
            updated_at=now,
        )
    else:
        row.document_json = _serialize(body)
        row.revision = next_revision
        row.updated_at = now
    session.add(row)
    session.commit()
    session.refresh(row)
    return row_document(row)


def delete_template(session: Session, user_id: int, template_id: str, expected_revision: int) -> int:
    _lock_user(session, user_id)
    if template_id in BUILTIN_TEMPLATES:
        raise TemplateConflictError("built-in templates cannot be deleted; use restore")
    row = _owned_row(session, user_id, template_id)
    if not row:
        raise TemplateNotFoundError("template not found")
    if row.revision != expected_revision:
        raise TemplateConflictError("template revision conflict")
    row.revision += 1
    row.deleted_at = time.time()
    row.updated_at = row.deleted_at
    session.add(row)
    session.commit()
    return row.revision


def restore_builtin(
    session: Session,
    user_id: int,
    template_id: str,
    expected_revision: int,
) -> TemplateDocument:
    builtin = _builtin_document(template_id)
    if builtin is None:
        raise TemplateNotFoundError("built-in template not found")
    _lock_user(session, user_id)
    row = _owned_row(session, user_id, template_id)
    current_revision = row.revision if row else BUILTIN_REVISION
    if current_revision != expected_revision:
        raise TemplateConflictError("template revision conflict")
    if row is None:
        return builtin

    canonical = _serialize(builtin)
    if row.document_json == canonical:
        return row_document(row)

    row.document_json = canonical
    row.revision += 1
    row.builtin_override = True
    row.updated_at = time.time()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row_document(row)
