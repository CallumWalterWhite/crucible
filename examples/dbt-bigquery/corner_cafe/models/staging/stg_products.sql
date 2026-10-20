select
    sku as product_sku,
    name as product_name,
    category,
    {{ cents_to_dollars('price_cents') }} as price,
    is_vegan
from {{ ref('raw_products') }}
