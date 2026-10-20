with sold as (
    select
        order_items.product_sku,
        sum(order_items.quantity) as units_sold
    from {{ ref('stg_order_items') }} as order_items
    inner join {{ ref('orders') }} as orders
        on order_items.order_id = orders.order_id
    where not orders.is_cancelled
    group by order_items.product_sku
)

select
    products.product_sku,
    products.product_name,
    products.category,
    products.price,
    products.is_vegan,
    coalesce(sold.units_sold, 0) as units_sold,
    coalesce(sold.units_sold, 0) * products.price as revenue
from {{ ref('stg_products') }} as products
left join sold
    on products.product_sku = sold.product_sku
