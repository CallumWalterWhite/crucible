{{ config(materialized='table') }}

select customer_id, name, credit_limit
from {{ ref('customers') }}
where is_vip
