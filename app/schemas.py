from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(Model):
    password: str = Field(min_length=1, max_length=512)


class CalendarCreate(Model):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=2000)
    source_type: Literal["ics", "webcal"] = "ics"
    kind: Literal["calendar", "school"] = "calendar"
    color: str = Field(default="#5c7cfa", min_length=4, max_length=20)
    enabled: bool = True


class CalendarUpdate(Model):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    url: Optional[str] = Field(default=None, min_length=8, max_length=2000)
    source_type: Optional[Literal["ics", "webcal"]] = None
    kind: Optional[Literal["calendar", "school"]] = None
    color: Optional[str] = Field(default=None, min_length=4, max_length=20)
    enabled: Optional[bool] = None


class BirthdayCreate(Model):
    name: str = Field(min_length=1, max_length=120)
    birth_date: date
    color: str = Field(default="#f59e0b", min_length=4, max_length=20)
    notes: str = Field(default="", max_length=2000)


class BirthdayUpdate(Model):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    birth_date: Optional[date] = None
    color: Optional[str] = Field(default=None, min_length=4, max_length=20)
    notes: Optional[str] = Field(default=None, max_length=2000)


class MemberCreate(Model):
    name: str = Field(min_length=1, max_length=120)
    color: str = Field(default="#5c7cfa", min_length=4, max_length=20)
    avatar_url: Optional[str] = Field(default=None, max_length=2000)


class MemberUpdate(Model):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    color: Optional[str] = Field(default=None, min_length=4, max_length=20)
    avatar_url: Optional[str] = Field(default=None, max_length=2000)
    sort_order: Optional[int] = Field(default=None, ge=0, le=10000)


class FrameCreate(Model):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=2000)
    height: int = Field(default=320, ge=160, le=1400)
    accent: str = Field(default="#5c7cfa", min_length=4, max_length=20)
    visible: bool = True


class FrameUpdate(Model):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    url: Optional[str] = Field(default=None, min_length=8, max_length=2000)
    height: Optional[int] = Field(default=None, ge=160, le=1400)
    accent: Optional[str] = Field(default=None, min_length=4, max_length=20)
    visible: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, ge=0, le=10000)


class NoteCreate(Model):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=10000)
    color: str = Field(default="#64748b", min_length=4, max_length=20)
    pinned: bool = False


class NoteUpdate(Model):
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    body: Optional[str] = Field(default=None, max_length=10000)
    color: Optional[str] = Field(default=None, min_length=4, max_length=20)
    pinned: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, ge=0, le=10000)


class ChecklistCreate(Model):
    text: str = Field(min_length=1, max_length=500)
    due_date: Optional[date] = None


class ChecklistUpdate(Model):
    text: Optional[str] = Field(default=None, min_length=1, max_length=500)
    done: Optional[bool] = None
    due_date: Optional[date] = None
    sort_order: Optional[int] = Field(default=None, ge=0, le=10000)


class SettingsUpdate(Model):
    display_name: Optional[str] = Field(default=None, max_length=120)
    header_title: Optional[str] = Field(default=None, max_length=120)
    greeting: Optional[str] = Field(default=None, max_length=240)
    timezone: Optional[str] = Field(default=None, max_length=80)
    location_name: Optional[str] = Field(default=None, max_length=120)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    temperature_unit: Optional[Literal["celsius", "fahrenheit"]] = None
    show_seconds: Optional[bool] = None
    weather_enabled: Optional[bool] = None
    theme: Optional[str] = Field(default=None, max_length=40)
