from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class AlertLevel(str, Enum):
    WARNING = "warning"
    CRITICAL = "critical"
    OUT_OF_STOCK = "out_of_stock"


class InventoryItem(BaseModel):
    account_name: str = "default"
    marketplace: str = "amazon"
    asin: str
    seller_sku: str
    # Amazon内部の在庫識別子。同じASINでも fn_sku が違えば別の在庫。
    # 逆に fn_sku が同じSKUは同じ在庫なので、足すと二重計上になる。
    fn_sku: str = ""
    product_name: str = ""
    condition: str = "NewItem"
    fulfillable_quantity: int = 0
    inbound_working_quantity: int = 0
    inbound_shipped_quantity: int = 0
    inbound_receiving_quantity: int = 0
    reserved_quantity: int = 0
    # 予約の内訳。移送中とFC作業中は「Amazonにあるが今は売れない」だけで
    # 自社の在庫。注文確定待ちは出ていく分なので在庫には数えない。
    pending_transshipment_quantity: int = 0
    fc_processing_quantity: int = 0
    pending_customer_order_quantity: int = 0
    unfulfillable_quantity: int = 0
    total_quantity: int = 0
    last_updated: datetime = Field(default_factory=datetime.utcnow)


class ThresholdConfig(BaseModel):
    reorder_point: int = 50
    critical_level: int = 10
    lead_time_days: int = 14
    safety_stock_multiplier: float = 1.5


class AlertEvent(BaseModel):
    seller_sku: str
    asin: str
    product_name: str
    current_quantity: int
    reorder_point: int
    level: AlertLevel
    message: str
    suggested_reorder_qty: Optional[int] = None
    avg_daily_sales: Optional[float] = None
    estimated_days_of_stock: Optional[float] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SalesVelocity(BaseModel):
    seller_sku: str
    asin: str
    period_days: int
    total_units_sold: int
    avg_daily_units: float
    trend_direction: str = "stable"
