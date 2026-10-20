with items as (
    select
        order_items.order_id,
        sum(order_items.quantity) as item_count,
        sum(order_items.quantity * products.price) as order_total
    from {{ ref('stg_order_items') }} as order_items
    inner join {{ ref('stg_products') }} as products
        on order_items.product_sku = products.product_sku
    group by order_items.order_id
)

select
    orders.order_id,
    orders.customer_id,
    orders.ordered_at,
    orders.ordered_date,
    orders.status,
    orders.is_cancelled,
    coalesce(items.item_count, 0) as item_count,
    coalesce(items.order_total, 0) as order_total
from {{ ref('stg_orders') }} as orders
left join items
    on orders.order_id = items.order_id
