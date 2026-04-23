# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

import math

from datetime import datetime, timedelta
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class RegisterPeerRequest(BaseModel):
    network_name: str
    subnet_offset: int = 1
    note: Optional[str] = None


class RegisterWireguardKeyRequest(BaseModel):
    wg_public_key: str


class SetPeerActiveRequest(BaseModel):
    wg_ip: str
    is_active: bool


class DeletePeerRequest(BaseModel):
    wg_ip: str


class DeleteNetworkRequest(BaseModel):
    network_name: str


class CreateNetworkRequest(BaseModel):
    name: str
    wg_public_ip: str
    wg_port: Optional[int] = Field(default=None, ge=1, le=65535)


class UpdateNetworkEndpointRequest(BaseModel):
    wg_public_ip: str
    wg_port: int = Field(ge=1, le=65535)


class RegisterSSHKeyRequest(BaseModel):
    username: str
    uid: int
    ssh_public_key: str
    key_type: str
    key_size: int
    key_comment: Optional[str] = None
    fingerprint: str
    expires_at: Optional[datetime] = None


class ClientStatusRequest(BaseModel):
    hostname: str
    uptime_seconds: int
    disk_available_gb: float
    memory_used_mb: int
    memory_total_mb: int
    load_1m: float
    load_5m: Optional[float] = None
    load_15m: Optional[float] = None
    version: str
    status_message: Optional[str] = None


class HardwareProfile(BaseModel):
    hostname: str
    model: str
    kernel_version: str
    cpu: dict
    firmware: dict
    memory: dict
    disks: dict
    usb_devices: str
    network_interfaces: dict


class LocationUpdateRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


def _validate_utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be RFC3339 UTC")
    return value


class I2CReadingUploadEntry(BaseModel):
    id: int = Field(ge=0)
    timestamp: datetime
    device_address: str
    sensor_type: str
    key: str
    value: float

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _validate_utc_timestamp(value)

    @field_validator("device_address")
    @classmethod
    def validate_device_address(cls, value: str) -> str:
        if not value.startswith("0x"):
            raise ValueError("device_address must start with 0x")
        int(value[2:], 16)
        return value.lower()

    @field_validator("value")
    @classmethod
    def validate_numeric_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        return value


class I2CReadingsUploadRequest(BaseModel):
    schema_version: Literal[1]
    hostname: str
    client_version: str
    batch_id: int = Field(ge=0)
    sent_at: datetime
    ownership_mode: Literal["client-retains", "server-owns"]
    reading_count: int = Field(ge=1)
    first_reading_id: int = Field(ge=0)
    last_reading_id: int = Field(ge=0)
    first_recorded_at: datetime
    last_recorded_at: datetime
    readings: list[I2CReadingUploadEntry] = Field(min_length=1)

    @field_validator("sent_at", "first_recorded_at", "last_recorded_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _validate_utc_timestamp(value)

    @model_validator(mode="after")
    def validate_batch_metadata(self):
        readings = self.readings
        if self.reading_count != len(readings):
            raise ValueError("reading_count must equal the number of readings")

        reading_ids = [reading.id for reading in readings]
        recorded_times = [reading.timestamp for reading in readings]
        if self.first_reading_id != min(reading_ids):
            raise ValueError("first_reading_id must match the minimum reading id")
        if self.last_reading_id != max(reading_ids):
            raise ValueError("last_reading_id must match the maximum reading id")
        if self.first_recorded_at != min(recorded_times):
            raise ValueError(
                "first_recorded_at must match the earliest reading timestamp"
            )
        if self.last_recorded_at != max(recorded_times):
            raise ValueError(
                "last_recorded_at must match the latest reading timestamp"
            )
        if self.first_recorded_at > self.last_recorded_at:
            raise ValueError("first_recorded_at must not be after last_recorded_at")
        return self


class BirdNETDetectionUploadEntry(BaseModel):
    source_path: str
    channel_index: int = Field(ge=0)
    window_index: int = Field(ge=0)
    max_score_start_frame: int = Field(ge=0)
    label: str
    score: float
    likely_score: Optional[float] = None
    volume: Optional[float] = Field(default=None, ge=0, le=1)
    clip_start_time: datetime
    clip_end_time: datetime

    @field_validator("clip_start_time", "clip_end_time")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _validate_utc_timestamp(value)

    @field_validator("volume", "score", "likely_score")
    @classmethod
    def validate_scores(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return value
        if not math.isfinite(value):
            raise ValueError("score must be finite")
        return value

    @model_validator(mode="after")
    def validate_times(self):
        if self.clip_start_time > self.clip_end_time:
            raise ValueError("clip_start_time must not be after clip_end_time")
        return self


class BirdNETResultsUploadRequest(BaseModel):
    schema_version: Literal[2]
    hostname: str
    client_version: str
    sent_at: datetime
    detections: list[BirdNETDetectionUploadEntry] = Field(min_length=1)

    @field_validator("sent_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _validate_utc_timestamp(value)
