"""media_library ORM：可授权媒体目录。

Day1 只建结构，不导入任何媒体；CHECK 保证仅 licensed/teaching/public_domain 可进入 active。
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import MediaCopyrightStatus, MediaStatus, MediaType


class MediaLibrary(TimestampMixin, Base):
    __tablename__ = "media_library"
    __table_args__ = (
        CheckConstraint(
            "type IN ('{0}', '{1}')".format(MediaType.OPERA, MediaType.MOVIE),
            name="ck_media_library_type",
        ),
        CheckConstraint(
            "copyright_status IN ('{0}', '{1}', '{2}', '{3}', '{4}', '{5}')".format(
                MediaCopyrightStatus.UNKNOWN,
                MediaCopyrightStatus.LICENSED,
                MediaCopyrightStatus.TEACHING,
                MediaCopyrightStatus.PUBLIC_DOMAIN,
                MediaCopyrightStatus.RESTRICTED,
                MediaCopyrightStatus.EXPIRED,
            ),
            name="ck_media_library_copyright_status",
        ),
        CheckConstraint(
            "status IN ('{0}', '{1}')".format(MediaStatus.ACTIVE, MediaStatus.HIDDEN),
            name="ck_media_library_status",
        ),
        CheckConstraint(
            "status <> '{0}' OR copyright_status IN ('{1}', '{2}', '{3}')".format(
                MediaStatus.ACTIVE,
                MediaCopyrightStatus.LICENSED,
                MediaCopyrightStatus.TEACHING,
                MediaCopyrightStatus.PUBLIC_DOMAIN,
            ),
            name="ck_media_library_active_requires_licensed",
        ),
        Index("ix_media_library_type_status", "type", "status"),
        Index("ix_media_library_title", "title"),
    )

    media_id: Mapped[uuid.UUID] = pk_uuid()
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    genre: Mapped[str | None] = mapped_column(String(80), nullable=True)
    era: Mapped[str | None] = mapped_column(String(80), nullable=True)
    artist: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # 未授权媒体不填地址；media_uri 可空
    media_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    copyright_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=MediaCopyrightStatus.UNKNOWN,
        server_default=MediaCopyrightStatus.UNKNOWN,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MediaStatus.HIDDEN, server_default=MediaStatus.HIDDEN
    )

