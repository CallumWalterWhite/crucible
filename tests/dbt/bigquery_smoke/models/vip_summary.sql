{{ config(materialized='view') }}

select count(*) as vip_count, sum(credit_limit) as total_credit
from {{ ref('vip_customers') }}
