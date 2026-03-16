#!/usr/bin/env python
"""Foreign Key Join example.

Demonstrates joining two tables via a foreign key using Faust's
subscription/response protocol (inspired by Kafka Streams KIP-213).

Scenario
========

We have **orders** referencing a **product_id**, and a separate
**products** table.  Whenever an order arrives or a product is updated,
the foreign key join emits a ``JoinedValue(left=order, right=product)``
so downstream consumers always see the latest combined data.

Quick Start
===========

1) Start a Kafka broker on ``localhost:9092``.

2) Start the worker:

.. sourcecode:: console

    $ python examples/foreign_key_join.py worker -l info

3) In another terminal, produce sample data:

.. sourcecode:: console

    $ python examples/foreign_key_join.py produce
"""
import asyncio
from itertools import count
import faust

app = faust.App(
    'faust-fk-join-example',
    broker='kafka://localhost:9092',
    store='rocksdb://',
    version=1,
    topic_partitions=4,
)


# --- Models ---

class Product(faust.Record, serializer='json'):
    name: str
    price: float


class Order(faust.Record, serializer='json'):
    product_id: str
    quantity: int


# --- Topics ---

orders_topic = app.topic('orders', value_type=Order)
products_topic = app.topic('products', value_type=Product)


# --- Tables ---

products_table = app.Table(
    'products',
    default=None,
    value_type=Product,
    help='Product catalog (product_id -> Product)',
)

orders_table = app.Table(
    'orders',
    default=None,
    value_type=Order,
    help='Orders (order_id -> Order)',
)


# --- Foreign Key Join ---

# Join orders to products using the product_id foreign key.
# Returns a channel emitting JoinedValue(left=Order, right=Product)
# whenever either side changes.
joined_channel = orders_table.foreign_key_join(
    products_table,
    extractor=lambda order: order.product_id,
    inner=True,
)


# --- Agents ---

@app.agent(products_topic)
async def ingest_products(products):
    """Populate the products table from the products topic."""
    async for product in products:
        products_table[product.name] = product


@app.agent(orders_topic)
async def ingest_orders(orders):
    """Populate the orders table from the orders topic."""
    async for order in orders:
        order_id = f'order-{order.product_id}-{order.quantity}'
        orders_table[order_id] = order


@app.agent(joined_channel)
async def process_joined(joined_values):
    """React to joined order+product pairs."""
    async for joined in joined_values:
        order = joined.left
        product = joined.right
        total = product.price * order.quantity
        print(
            f'Joined: {order.quantity}x {product.name} '
            f'@ ${product.price:.2f} = ${total:.2f}'
        )


# --- CLI command to produce sample data ---

@app.command()
async def produce(self):
    """Send sample products and orders."""
    # Seed products
    sample_products = [
        ('widget', Product(name='widget', price=9.99)),
        ('gadget', Product(name='gadget', price=24.99)),
        ('gizmo', Product(name='gizmo', price=4.50)),
    ]
    for key, product in sample_products:
        await products_topic.send(key=key, value=product)
        print(f'Produced product: {key} -> {product}')

    # Give products time to land
    await asyncio.sleep(2.0)

    # Send orders referencing products by product_id
    sample_orders = [
        Order(product_id='widget', quantity=3),
        Order(product_id='gadget', quantity=1),
        Order(product_id='gizmo', quantity=10),
        Order(product_id='widget', quantity=7),
    ]
    for i, order in enumerate(sample_orders, 1):
        await orders_topic.send(key=f'order-{i}', value=order)
        print(f'Produced order #{i}: {order}')

    # Send a product update — should propagate to all subscribers
    await asyncio.sleep(2.0)
    updated = Product(name='widget', price=7.99)
    await products_topic.send(key='widget', value=updated)
    print(f'Updated product: widget -> {updated}')


if __name__ == '__main__':
    app.main()
