select
    id as order_id,
    customer_id,
    ordered_at,
    date(ordered_at) as ordered_date,
    status,
    status = 'cancelled' as is_cancelled
from {{ ref('raw_orders') }}
