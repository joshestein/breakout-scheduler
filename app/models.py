from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (ForeignKey, String, Integer, Boolean, Float, DateTime,
                        UniqueConstraint, Text, false, true)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    participant_code: Mapped[str] = mapped_column(String(100), index=True)
    admin_code: Mapped[str] = mapped_column(String(100), index=True)
    # whether participants may attach a description when proposing (admin-toggled)
    descriptions_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Room(Base):
    __tablename__ = "rooms"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    capacity: Mapped[int] = mapped_column(Integer)


class Participant(Base):
    __tablename__ = "participants"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # organizer identity
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    # the round this proposal was collected for; proposals + their votes are
    # scoped to one round and do not carry into the next (re-add as new instead).
    round_id: Mapped[int | None] = mapped_column(ForeignKey("rounds.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|retired
    created_by: Mapped[int] = mapped_column(ForeignKey("participants.id"))
    min_interest_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_attendees: Mapped[int | None] = mapped_column(Integer, nullable=True)  # host seat cap (small workshop)
    merged_into: Mapped[int | None] = mapped_column(
        ForeignKey("proposals.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ProposalRunner(Base):
    __tablename__ = "proposal_runners"
    __table_args__ = (UniqueConstraint("proposal_id", "participant_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), index=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("participant_id", "proposal_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), index=True)
    stars: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Round(Base):
    __tablename__ = "rounds"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="collecting")  # draft|collecting|published|archived
    lambda_fairness: Mapped[float] = mapped_column(Float, default=3.0)
    min_interest: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RoundParticipant(Base):
    __tablename__ = "round_participants"
    __table_args__ = (UniqueConstraint("round_id", "participant_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    present: Mapped[bool] = mapped_column(Boolean, default=True)


class Timeslot(Base):
    __tablename__ = "timeslots"
    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(Integer)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=45)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TimeslotRoom(Base):
    __tablename__ = "timeslot_rooms"
    __table_args__ = (UniqueConstraint("timeslot_id", "room_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    timeslot_id: Mapped[int] = mapped_column(ForeignKey("timeslots.id"))
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"))


class Placement(Base):
    __tablename__ = "placements"
    __table_args__ = (UniqueConstraint("timeslot_id", "room_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    timeslot_id: Mapped[int] = mapped_column(ForeignKey("timeslots.id"), index=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"))
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    __table_args__ = (UniqueConstraint("endpoint"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    endpoint: Mapped[str] = mapped_column(Text)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class RoundSnapshot(Base):
    __tablename__ = "round_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    stats_json: Mapped[str] = mapped_column(Text)
    grid_json: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
