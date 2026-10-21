{{ config(
    materialized='incremental',
    incremental_strategy='microbatch',
    event_time='event_date',
    batch_size='day',
    begin='2024-01-01',
    partition_by={'field': 'event_date', 'data_type': 'date', 'granularity': 'day'}
) }}

select event_date, id, amount from {{ source('raw', 'events') }}
