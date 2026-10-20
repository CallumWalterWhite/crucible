with kept_orders as (
    select * from {{ ref('orders') }}
    where not is_cancelled
)

select
    customers.customer_id,
    customers.customer_name,
    customers.is_subscriber,
    count(kept_orders.order_id) as order_count,
    coalesce(sum(kept_orders.order_total), 0) as lifetime_spend,
    min(kept_orders.ordered_at) as first_order_at,
    max(kept_orders.ordered_at) as last_order_at,
    if(count(kept_orders.order_id) > 1, 'returning', 'new') as customer_type
from {{ ref('stg_customers') }} as customers
left join kept_orders
    on customers.customer_id = kept_orders.customer_id
group by customers.customer_id, customers.customer_name, customers.is_subscriber
