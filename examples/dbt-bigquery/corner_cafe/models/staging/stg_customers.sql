select
    id as customer_id,
    name as customer_name,
    signed_up_at,
    is_subscriber
from {{ ref('raw_customers') }}
