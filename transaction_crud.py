"""전월세 거래 수동 CRUD API. Fact Sheet 근거로 사용된 원자료 변경은 차단한다."""

import json
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, DataError

router = APIRouter(prefix="/api/transactions", tags=["전월세 거래 CRUD"])


class TransactionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_type: str = Field(default="OFFICETEL", min_length=1, max_length=30)
    district_code: str = Field(default="11620", min_length=5, max_length=5)
    district_name: str = Field(default="관악구", min_length=1, max_length=50)
    building_name: str | None = Field(default=None, max_length=200)
    contract_date: date
    area_sqm: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    deposit_manwon: int = Field(ge=0)
    rent_manwon: int = Field(ge=0)
    floor_no: int | None = Field(default=None, ge=-32768, le=32767)
    source_row_key: str = Field(min_length=1, max_length=128)


class TransactionOutput(TransactionInput):
    id: int
    collected_at: datetime


def make_crud_router(engine):
    """기존 app.py의 DB 엔진을 재사용한다."""

    def get_one(conn, transaction_id, lock=False):
        sql = """
            SELECT id, property_type, district_code, district_name,
                   building_name, contract_date, area_sqm, deposit_manwon,
                   rent_manwon, floor_no, source_row_key, collected_at
            FROM rental_transactions WHERE id = :id
        """
        if lock:
            sql += " FOR UPDATE"
        row = conn.execute(text(sql), {"id": transaction_id}).mappings().first()
        if row is None:
            raise HTTPException(404, "해당 ID의 거래를 찾을 수 없습니다.")
        return dict(row)

    def protect_evidence(conn, transaction_id):
        # Fact Sheet evidence_json.transaction_ids에 저장된 ID 사용 여부 확인.
        used = conn.execute(text("""
            SELECT id FROM fact_sheets
            WHERE JSON_CONTAINS(
                JSON_EXTRACT(evidence_json, '$.transaction_ids'),
                CAST(:transaction_id AS JSON)
            )
            LIMIT 1
        """), {"transaction_id": str(transaction_id)}).first()
        if used:
            raise HTTPException(
                409,
                "이 거래는 Fact Sheet의 통계 근거이므로 수정하거나 삭제할 수 없습니다."
            )

    def convert_input(payload):
        fields = payload.model_dump()
        fields["raw_data"] = json.dumps({
            "manual_entry": True,
            "contract_date": fields["contract_date"].isoformat(),
            "area_sqm": str(fields["area_sqm"]),
            "source_row_key": fields["source_row_key"],
        }, ensure_ascii=False)
        return fields

    @router.post("", response_model=TransactionOutput, status_code=status.HTTP_201_CREATED)
    def create_transaction(payload: TransactionInput):
        params = convert_input(payload)
        try:
            with engine.begin() as conn:
                result = conn.execute(text("""
                    INSERT INTO rental_transactions
                    (property_type, district_code, district_name, building_name,
                     contract_date, area_sqm, deposit_manwon, rent_manwon,
                     floor_no, source_row_key, raw_data, collected_at)
                    VALUES
                    (:property_type, :district_code, :district_name, :building_name,
                     :contract_date, :area_sqm, :deposit_manwon, :rent_manwon,
                     :floor_no, :source_row_key, :raw_data, NOW())
                """), params)
                return get_one(conn, result.lastrowid)
        except (IntegrityError, DataError) as exc:
            raise HTTPException(400, "거래를 저장할 수 없습니다. 중복된 거래 식별키나 DB 제약조건을 확인해 주세요.") from exc

    @router.get("", response_model=list[TransactionOutput])
    def list_transactions(limit: int = 20, offset: int = 0):
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(400, "limit은 1~100, offset은 0 이상이어야 합니다.")
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, property_type, district_code, district_name,
                       building_name, contract_date, area_sqm, deposit_manwon,
                       rent_manwon, floor_no, source_row_key, collected_at
                FROM rental_transactions
                ORDER BY id DESC LIMIT :limit OFFSET :offset
            """), {"limit": limit, "offset": offset}).mappings().all()
        return [dict(row) for row in rows]

    @router.get("/{transaction_id}", response_model=TransactionOutput)
    def read_transaction(transaction_id: int):
        with engine.connect() as conn:
            return get_one(conn, transaction_id)

    @router.put("/{transaction_id}", response_model=TransactionOutput)
    def update_transaction(transaction_id: int, payload: TransactionInput):
        params = convert_input(payload)
        params["id"] = transaction_id
        try:
            with engine.begin() as conn:
                get_one(conn, transaction_id, lock=True)
                protect_evidence(conn, transaction_id)
                conn.execute(text("""
                    UPDATE rental_transactions SET
                        property_type=:property_type, district_code=:district_code,
                        district_name=:district_name, building_name=:building_name,
                        contract_date=:contract_date, area_sqm=:area_sqm,
                        deposit_manwon=:deposit_manwon, rent_manwon=:rent_manwon,
                        floor_no=:floor_no, source_row_key=:source_row_key,
                        raw_data=:raw_data
                    WHERE id=:id
                """), params)
                return get_one(conn, transaction_id)
        except (IntegrityError, DataError) as exc:
            raise HTTPException(400, "거래를 수정할 수 없습니다. 중복된 거래 식별키나 DB 제약조건을 확인해 주세요.") from exc

    @router.delete("/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_transaction(transaction_id: int):
        with engine.begin() as conn:
            get_one(conn, transaction_id, lock=True)
            protect_evidence(conn, transaction_id)
            conn.execute(text("DELETE FROM rental_transactions WHERE id=:id"), {"id": transaction_id})
        return Response(status_code=204)

    return router
