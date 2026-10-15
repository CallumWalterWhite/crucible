{{ config(materialized='incremental', incremental_strategy='merge', unique_key='customer_id') }}

select customer_id, name, signup_date
from {{ ref('customers') }}
