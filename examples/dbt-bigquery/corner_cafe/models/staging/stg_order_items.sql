select
    id as order_item_id,
    order_id,
    sku as product_sku,
    quantity
from {{ ref('raw_order_items') }}
