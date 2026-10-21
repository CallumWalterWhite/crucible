{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    partition_by={'field': 'event_date', 'data_type': 'date'},
    copy_partitions=true
) }}

select event_date, id, amount from {{ source('raw', 'events') }}
