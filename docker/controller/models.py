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
    channel_index: int = Field(ge=0)
    window_index: int = Field(ge=0)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    start_sec: float = Field(ge=0)
    end_sec: float = Field(ge=0)
    top_label: str
    top_score: float
    top_likely_score: Optional[float] = None

    @field_validator("top_score", "top_likely_score")
    @classmethod
    def validate_scores(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return value
        if not math.isfinite(value):
            raise ValueError("score must be finite")
        return value

    @model_validator(mode="after")
    def validate_window(self):
        if self.start_frame > self.end_frame:
            raise ValueError("start_frame must not be after end_frame")
        if self.start_sec > self.end_sec:
            raise ValueError("start_sec must not be after end_sec")
        return self


class BirdNETFlacRunUploadEntry(BaseModel):
    channel_index: int = Field(ge=0)
    run_index: int = Field(ge=0)
    label: str
    label_dir: Optional[str] = None
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    start_sec: float = Field(ge=0)
    end_sec: float = Field(ge=0)
    peak_score: float
    peak_likely_score: Optional[float] = None
    flac_path: str
    deleted_at: Optional[datetime] = None

    @field_validator("deleted_at")
    @classmethod
    def validate_deleted_at(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return value
        return _validate_utc_timestamp(value)

    @field_validator("peak_score", "peak_likely_score")
    @classmethod
    def validate_scores(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return value
        if not math.isfinite(value):
            raise ValueError("score must be finite")
        return value

    @model_validator(mode="after")
    def validate_window(self):
        if self.start_frame > self.end_frame:
            raise ValueError("start_frame must not be after end_frame")
        if self.start_sec > self.end_sec:
            raise ValueError("start_sec must not be after end_sec")
        return self


class BirdNETProcessedFileUploadEntry(BaseModel):
    source_path: str
    sample_rate: int = Field(ge=1)
    channels: int = Field(ge=1)
    frames: int = Field(ge=0)
    started_at: datetime
    processed_at: datetime
    status: str
    error: Optional[str] = None
    output_dir: Optional[str] = None
    deleted_source: bool
    detections: list[BirdNETDetectionUploadEntry] = Field(default_factory=list)
    flac_runs: list[BirdNETFlacRunUploadEntry] = Field(default_factory=list)

    @field_validator("started_at", "processed_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _validate_utc_timestamp(value)

    @model_validator(mode="after")
    def validate_metadata(self):
        if self.processed_at < self.started_at:
            raise ValueError("processed_at must not be before started_at")
        if self.status != "done":
            raise ValueError("status must be 'done'")
        return self


class BirdNETResultsUploadRequest(BaseModel):
    schema_version: Literal[1]
    hostname: str
    client_version: str
    batch_id: int = Field(ge=0)
    sent_at: datetime
    ownership_mode: Literal["client-retains", "server-owns"]
    source_count: int = Field(ge=1)
    first_source_path: str
    last_source_path: str
    first_processed_at: datetime
    last_processed_at: datetime
    processed_files: list[BirdNETProcessedFileUploadEntry] = Field(min_length=1)

    @field_validator("sent_at", "first_processed_at", "last_processed_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _validate_utc_timestamp(value)

    @model_validator(mode="after")
    def validate_batch_metadata(self):
        processed_files = self.processed_files
        if self.source_count != len(processed_files):
            raise ValueError("source_count must equal the number of processed_files")

        source_paths = [entry.source_path for entry in processed_files]
        processed_times = [entry.processed_at for entry in processed_files]
        if self.first_source_path != min(source_paths):
            raise ValueError("first_source_path must match the minimum source_path")
        if self.last_source_path != max(source_paths):
            raise ValueError("last_source_path must match the maximum source_path")
        if self.first_processed_at != min(processed_times):
            raise ValueError(
                "first_processed_at must match the earliest processed_at timestamp"
            )
        if self.last_processed_at != max(processed_times):
            raise ValueError(
                "last_processed_at must match the latest processed_at timestamp"
            )
        if self.first_processed_at > self.last_processed_at:
            raise ValueError("first_processed_at must not be after last_processed_at")
        return self
