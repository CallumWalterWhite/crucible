-- Fails with the offending orders if any order total is negative.
select order_id, order_total
from {{ ref('orders') }}
where order_total < 0
