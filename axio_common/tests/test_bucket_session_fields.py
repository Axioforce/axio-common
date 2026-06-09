from axio_common.models.bucket_session import (
    CalibrationBucketSession, CalibrationBucketSessionResponse,
)

def test_size_and_room_temp_columns_exist():
    cols = CalibrationBucketSession.__table__.columns
    assert "size" in cols and cols["size"].nullable is True
    assert "room_temp" in cols and cols["room_temp"].nullable is True

def test_response_dto_carries_size_and_room_temp():
    fields = CalibrationBucketSessionResponse.model_fields
    assert "size" in fields and "room_temp" in fields
