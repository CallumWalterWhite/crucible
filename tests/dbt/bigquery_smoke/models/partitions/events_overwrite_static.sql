{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    partition_by={'field': 'event_date', 'data_type': 'date'},
    partitions=['date(2024, 1, 2)']
) }}

select event_date, id, amount from {{ source('raw', 'events') }}
{% if is_incremental() %}
where event_date = date(2024, 1, 2)
{% endif %}
