-- Sample data for the demo database (run once after `terraform apply`).
create table if not exists customers (
    id bigint primary key,
    email text not null,
    created_at timestamptz not null default now()
);
create table if not exists products (
    id bigint primary key,
    name text not null,
    price_cents integer not null
);
create table if not exists orders (
    id bigint primary key,
    customer_id bigint not null references customers(id),
    product_id bigint not null references products(id),
    quantity integer not null,
    created_at timestamptz not null default now()
);

insert into customers (id, email)
select g, 'customer' || g || '@example.com' from generate_series(1, 2000) g
on conflict do nothing;

insert into products (id, name, price_cents)
select g, 'Demo product ' || g, 1000 + g * 7 from generate_series(1, 25) g
on conflict do nothing;

insert into orders (id, customer_id, product_id, quantity)
select g, 1 + (g * 7) % 2000, 1 + g % 25, 1 + g % 3 from generate_series(1, 10000) g
on conflict do nothing;
