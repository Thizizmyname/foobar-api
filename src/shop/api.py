import logging
import numpy as np
import math
from itertools import accumulate
from datetime import date, timedelta
from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import TruncDay
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from sklearn.svm import SVR
from .suppliers.base import SupplierAPIException
from . import models, enums, suppliers, exceptions

log = logging.getLogger(__name__)


@transaction.atomic
def create_product(code, name):
    """Create an product"""
    product_obj = models.Product(
        code=code,
        name=name
    )
    product_obj.save()
    return product_obj


@transaction.atomic
def update_product(id, **kwargs):
    product_obj = models.Product.objects.get(id=id)
    for k, v in kwargs.items():
        setattr(product_obj, k, v)
    product_obj.save()


def get_product(id):
    """Return item with given id.

    Returns None if the product does not exist.
    """
    try:
        return models.Product.objects.get(id=id)
    except models.Product.DoesNotExist:
        return None


def get_product_transactions_by_ref(reference):
    """Return item transactions with given reference."""
    ct = ContentType.objects.get_for_model(reference)
    qs = models.ProductTransactionStatus.objects.filter(
        reference_ct=ct,
        reference_id=reference.pk,
    ).values_list('trx', flat=True).distinct()

    return models.ProductTransaction.objects.filter(pk__in=qs).distinct()


@transaction.atomic
def create_product_transaction(product_id, trx_type, qty, reference=None):
    """
    Create item transaction for given item.

    It automagically takes care of updating the quantity for the product.
    """
    product_obj = models.Product.objects.get(id=product_id)
    ct = None
    if reference is not None:
        ct = ContentType.objects.get_for_model(reference)
    trx_obj = product_obj.transactions.create(trx_type=trx_type, qty=qty)
    trx_obj.states.create(
        status=enums.TrxStatus.PENDING,  # It's here to avoid any confusion
        reference_ct=ct,
        reference_id=reference.pk if reference is not None else None
    )
    return trx_obj


@transaction.atomic
def finalize_product_transaction(trx_id, reference=None):
    trx_obj = models.ProductTransaction.objects.get(pk=trx_id)
    trx_obj.set_status(enums.TrxStatus.FINALIZED, reference)


@transaction.atomic
def cancel_product_transaction(trx_id, reference=None):
    trx_obj = models.ProductTransaction.objects.get(id=trx_id)
    trx_obj.set_status(enums.TrxStatus.CANCELED, reference)


def list_products(start=None, limit=None, **kwargs):
    """Returns a list of products matching the criteria.

    Criteria should be passed to the function as keyword arguments.
    Criteria arguments support Django field lookups.
    """
    return models.Product.objects.filter(**kwargs)[start:limit]


def list_categories():
    return models.ProductCategory.objects.all()


@transaction.atomic
def get_supplier_product(supplier_id, sku, refresh=False):
    """Returns supplier product for given SKU.

    If the product does not exist in the local database, fetch it from the
    supplier.
    """
    if not refresh:
        try:
            return models.SupplierProduct.objects.get(
                supplier_id=supplier_id,
                sku=sku
            )
        except models.SupplierProduct.DoesNotExist:
            pass

    # Product has not been found in the database. Let's fetch it from
    # the supplier.
    supplier_obj = models.Supplier.objects.get(id=supplier_id)
    supplier_api = suppliers.get_supplier_api(supplier_obj.internal_name)
    product_data = supplier_api.retrieve_product(sku)
    if product_data is None:
        log.warning('Product not found (sku: %s, supplier: %s',
                    sku, supplier_id)
        return None
    product_obj, _ = models.SupplierProduct.objects.update_or_create(
        supplier_id=supplier_id,
        sku=sku,
        defaults={
            'price': product_data.price,
            'name': product_data.name,
            'units': product_data.units,
        }
    )
    return product_obj


def parse_report(supplier_internal_name, report_path):
    """Parses a report file and returns parsed items."""
    supplier_api = suppliers.get_supplier_api(supplier_internal_name)
    return supplier_api.parse_delivery_report(report_path)


@transaction.atomic
def populate_delivery(delivery_id):
    """Populates the delivery with products based on the imported report."""
    delivery_obj = models.Delivery.objects.get(id=delivery_id)
    supplier_obj = delivery_obj.supplier
    items = parse_report(supplier_obj.internal_name, delivery_obj.report.path)
    for item in items:
        product_obj = get_supplier_product(supplier_obj.id, item.sku)
        if product_obj is not None:
            models.DeliveryItem.objects.create(
                delivery=delivery_obj,
                supplier_product_id=product_obj.id,
                qty=item.qty * product_obj.qty_multiplier,
                price=item.price / product_obj.qty_multiplier
            )
    return delivery_obj


@transaction.atomic
def process_delivery(delivery_id):
    """Adjusts the stock quantities based on the delivery data."""
    delivery_obj = models.Delivery.objects.get(id=delivery_id)
    assert delivery_obj.valid, ('Some of the delivered items are not '
                                'associated with a product in the system.')
    for item in delivery_obj.delivery_items.all():
        supplier_product = item.supplier_product
        create_product_transaction(
            product_id=supplier_product.product.id,
            trx_type=enums.TrxType.INVENTORY,
            qty=item.qty,
            reference=item
        )
    delivery_obj.locked = True
    delivery_obj.save()


@transaction.atomic
def initiate_stocktaking(chunk_size=10):
    """Initiates a stock-taking procedure for all the products."""
    stocktake_qs = models.Stocktake.objects
    # Make sure that there is no stock-taking in progress
    if not stocktake_qs.filter(locked=False).count() == 0:
        raise exceptions.APIException('Stock-taking already in progress.')
    stocktake_obj = stocktake_qs.create()
    # Order products by category, so that chunk contain mostly that share
    # category. Products in the same category are most often placed near each
    # other, which should make the process of stock-taking more effective.
    product_objs = list(models.Product.objects.all().order_by('category'))
    for i in range(0, len(product_objs), chunk_size):
        chunk_obj = stocktake_obj.chunks.create()
        chunk_products = product_objs[i:i + chunk_size]
        for p in chunk_products:
            chunk_obj.items.create(product=p)
    return stocktake_obj


@transaction.atomic
def finalize_stocktaking(stocktake_id):
    """Applies the result of stock taking to the stock quantities."""
    stocktake_obj = models.Stocktake.objects.get(id=stocktake_id)
    if stocktake_obj.locked:
        raise exceptions.APIException('Stock-taking already finished.')
    # Make sure that all the chunks are finished
    chunk_objs = stocktake_obj.chunks.all()
    if not all(obj.locked for obj in chunk_objs):
        raise exceptions.APIException('Found unfinished chunks.')
    for chunk_obj in chunk_objs:
        for item_obj in chunk_obj.items.all():
            product_obj = item_obj.product
            create_product_transaction(
                product_id=product_obj.id,
                trx_type=enums.TrxType.CORRECTION,
                qty=item_obj.qty - product_obj.qty,
                reference=item_obj
            )
    stocktake_obj.locked = True
    stocktake_obj.save()
    return stocktake_obj


def finalize_stocktake_chunk(chunk_id):
    """Marks given chunk as finished."""
    chunk_obj = models.StocktakeChunk.objects.get(id=chunk_id)
    if chunk_obj.locked:
        raise exceptions.APIException('Chunk already locked.')
    chunk_obj.locked = True
    chunk_obj.owner = None
    chunk_obj.save()


@transaction.atomic
def assign_free_stocktake_chunk(user_id, stocktake_id):
    """Assigns a free stock-take chunk to a user, if any free left.

    If user is already assigned to a chunk, that chunk should be returned.
    """
    chunk_qs = models.StocktakeChunk.objects.select_for_update()
    try:
        return chunk_qs.get(
            stocktake_id=stocktake_id,
            owner_id=user_id
        )
    except models.StocktakeChunk.DoesNotExist:
        pass
    chunk_objs = chunk_qs.filter(
        stocktake_id=stocktake_id,
        locked=False,
        owner__isnull=True
    )
    if not chunk_objs:
        return None
    chunk_obj = chunk_objs.first()
    chunk_obj.owner_id = user_id
    chunk_obj.save()
    return chunk_obj


@transaction.atomic
def predict_quantity(product_id, target, current_date=None):
    """Predicts when a product will reach the target quantity."""
    product_obj = models.Product.objects.get(id=product_id)
    if product_obj.qty <= target:
        # No prediction if already at the target quantity.
        return None

    # Find the last restock transaction
    qs = models.ProductTransaction.objects.finalized()
    restock_trx = qs.restocks().order_by('-date_created').first()
    if restock_trx is None:
        # The product has never been restocked.
        return None

    initial_qty = qs \
        .filter(date_created__lte=restock_trx.date_created) \
        .aggregate(qty=Sum('qty'))['qty'] or 0
    trx_objs = qs \
        .filter(date_created__gt=restock_trx.date_created) \
        .annotate(date=TruncDay('date_created')) \
        .values('date') \
        .annotate(aggregated_qty=Sum('qty')) \
        .values('date', 'aggregated_qty')
    if not trx_objs:
        # No data points to base the prediction on.
        return None

    today_ordinal = (current_date or date.today()).toordinal()
    date_offset = trx_objs[0]['date'].toordinal()

    # At this point we want to generate data-points that we will feed into a
    # Epsilon-Support Vector Regression model. Initially, the data-points
    # look like following:
    #
    # +---+----+-----+----+----+
    # | x | 0  |  1  | 2  | 4  |
    # +---+----+-----+----+----+
    # | y | -5 | -10 | -2 | -3 |
    # +---+----+-----+----+----+
    #
    # We want however to include the initial quantity and we do that by adding
    # it at x = -1:
    #
    # +---+-----+----+-----+----+----+
    # | x | -1  | 0  |  1  | 2  | 4  |
    # +---+-----+----+-----+----+----+
    # | y | 100 | -5 | -10 | -2 | -3 |
    # +---+-----+----+-----+----+----+
    #
    # In the final step, we want to convert all the values at x >= 0 into
    # actuall quantity levels, not just differences, so the data looks like
    # this:
    #
    # +---+-----+----+----+----+----+
    # | x | -1  | 0  | 1  | 2  | 4  |
    # +---+-----+----+----+----+----+
    # | y | 100 | 95 | 85 | 83 | 80 |
    # +---+-----+----+----+----+----+
    #
    # Also, in case of the stock quantity not changing for a couple of days
    # we insert additional data point at the current's day offset x
    # with the current quantity of the product.
    #
    # The cryptic code below does just that.
    x = [trx_obj['date'].toordinal() - date_offset for trx_obj in trx_objs]
    x = [-1] + x

    y = [trx_obj['aggregated_qty'] for trx_obj in trx_objs]
    y = [initial_qty] + y

    if today_ordinal not in x:
        x.append(today_ordinal - date_offset)
        y.append(0)

    x = np.asarray(x).reshape(-1, 1)
    y = np.asarray(list(accumulate(y)))

    # Fit the SVR model using above data. We rely here on the linear kernel as
    # our experiments showed that that gave the best results.
    svr = SVR(kernel='linear', C=1e2)
    svr.fit(x, y)
    if svr.coef_ >= 0:
        # The function is non-decreasing, so no prediction can be made.
        return None
    days = (-initial_qty / svr.coef_).astype(int).item()
    return date.fromordinal(date_offset + days)


@transaction.atomic
def update_out_of_stock_forecast(product_id):
    product_obj = models.Product.objects.get(id=product_id)
    product_obj.out_of_stock_forecast = predict_quantity(product_id, target=0)
    product_obj.save()


def order_from_supplier(product_id, qty, supplier_id=None):
    """Orders the cheapest product from a supplier."""
    products = models.SupplierProduct.objects.filter(product_id=product_id)

    if supplier_id is not None:
        products = products.filter(supplier_id=supplier_id)

    # A product can be associated with several different supplier products from
    # the same supplier. Supplier products are also most often sold in batches,
    # which means you usually cannot purchase the exact amount you need.
    # Following section of the code takes care of calculating the minimum cost
    # of each product while taking `qty` into account. It will then try to
    # purchase the cheapest one and if not possible (for example out of stock),
    # continue onto next one.
    def minimum_qty(sp):
        return math.ceil(qty / sp.qty)

    def cost(sp):
        # Calculate the minimum quantity of the supplier product that needs to
        # be purchased in order to reach `qty`.
        return minimum_qty(sp) * sp.qty * sp.unit_price

    products = sorted(products, key=cost)
    for product in products:
        supplier = product.supplier
        supplier_api = suppliers.get_supplier_api(supplier.internal_name)
        try:
            supplier_api.order_product(product.sku, minimum_qty(product))
            return product
        except SupplierAPIException:
            # Log the error and try the next product.
            log.warning('Failed to order product SKU %d from %s.',
                        product.sku, supplier.internal_name)
    else:
        msg = 'Could not order {}.'.format(product.sku)
        raise exceptions.APIException(msg)


def order_refill(supplier_id, current_date=None):
    """Orders products that will run out of stock before the next delivery."""
    def next_weekday(d, weekday):
        days_ahead = weekday - d.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return d + timedelta(days_ahead)

    today = current_date or timezone.now().date()
    supplier = models.Supplier.objects.get(id=supplier_id)
    delivery_weekday = supplier.delivers_on
    first_delivery = next_weekday(today, delivery_weekday)
    second_delivery = next_weekday(first_delivery, delivery_weekday)
    # Get base stock levels for the products that will run out of the stock
    # before the second delivery.
    base_levels = models.BaseStockLevel.objects.filter(
        product__out_of_stock_forecast__lt=second_delivery
    )
    ordered = []
    for base_level in base_levels:
        sp = order_from_supplier(
            base_level.product.id,
            base_level.level,
            supplier_id=supplier.id
        )
        ordered.append(sp)
    return ordered
