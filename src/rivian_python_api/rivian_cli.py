#!/usr/bin/env python
# encoding: utf-8
import sys
import argparse
import json
from rivian_api import *
from rivian_map import *
import pickle
from dateutil.parser import parse
from dateutil import tz
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

PICKLE_FILE = 'rivian_auth.pickle'


def save_state(rivian):
    state = {
        "_access_token": rivian._access_token,
        "_refresh_token": rivian._refresh_token,
        "_user_session_token": rivian._user_session_token,
    }
    with open(PICKLE_FILE, 'wb') as f:
        pickle.dump(state, f)


def restore_state(rivian):
    while True:
        try:
            rivian.create_csrf_token()
            break
        except Exception as e:
            time.sleep(5)

    RIVIAN_AUTHORIZATION = os.getenv('RIVIAN_AUTHORIZATION')
    if RIVIAN_AUTHORIZATION:
        rivian._access_token, rivian._refresh_token, rivian._user_session_token = RIVIAN_AUTHORIZATION.split(';')
    elif os.path.exists(PICKLE_FILE):
        with open(PICKLE_FILE, 'rb') as f:
            obj = pickle.load(f)
        rivian._access_token = obj['_access_token']
        rivian._refresh_token = obj['_refresh_token']
        rivian._user_session_token = obj['_user_session_token']
    else:
        raise Exception("Please log in first")


def get_rivian_object():
    rivian = Rivian()
    restore_state(rivian)
    return rivian


# Set from --raw in main(); makes dump_response() emit pretty JSON.
RAW = False


def gql_data(response_json, *path, default=None):
    """Safely walk a GraphQL response: response_json['data'][*path].

    Returns ``default`` if the response is not a dict, errored (no 'data'), or
    any key along the path is missing or None. This is the single guard against
    KeyError/TypeError when Rivian returns an error body, deprecates an
    endpoint, or stops sending a field. Adding/consuming new fields safely is
    just another gql_data() call.
    """
    if not isinstance(response_json, dict):
        return default
    node = response_json.get('data')
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return default if node is None else node


def dump_response(label, response_json):
    """Print a raw API response: pretty JSON under --raw, else legacy repr.

    Used for field discovery — run any command with --raw (or --verbose) to see
    exactly what Rivian returned, including newly added fields.
    """
    if RAW:
        print(json.dumps(response_json, indent=2, default=str))
    else:
        print(f"{label}:\n{response_json}")


def login_with_password(verbose):
    rivian = Rivian()
    try:
        rivian.login(os.getenv('RIVIAN_USERNAME'), os.getenv('RIVIAN_PASSWORD'))
    except Exception as e:
        if verbose:
            print(f"Authentication failed, check RIVIAN_USERNAME and RIVIAN_PASSWORD: {str(e)}")
        return None
    return rivian


def login_with_otp(verbose, otp_token):
    otpCode = input('Enter OTP: ')
    rivian = Rivian()
    try:
        rivian.login_with_otp(
            username=os.getenv('RIVIAN_USERNAME'),
            otpCode=otpCode,
            otpToken=otp_token)
    except Exception as e:
        if verbose:
            print(f"Authentication failed, OTP mismatch: {str(e)}")
        return None
    return rivian


def login(verbose):
    # Intentionally don't use same Rivian object for login and subsequent calls
    rivian = login_with_password(verbose)
    if not rivian:
        return
    if rivian.otp_needed:
        rivian = login_with_otp(verbose, otp_token=rivian._otp_token)
    if rivian:
        print("Login successful")
        save_state(rivian)
    return


def user_information(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_user_information()
    except Exception:
        if verbose:
            print("Error getting user information")
        return {}
    if verbose:
        dump_response("user_information", response_json)
    return gql_data(response_json, 'currentUser', default={}) or {}


def vehicle_orders(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.vehicle_orders()
    except:
        return []
    if verbose:
        dump_response("orders", response_json)
    orders = []
    for order in gql_data(response_json, 'orders', 'data', default=[]) or []:
        orders.append({
            'id': order['id'],
            'orderDate': order['orderDate'],
            'state': order['state'],
            'configurationStatus': order['configurationStatus'],
            'fulfillmentSummaryStatus': order['fulfillmentSummaryStatus'],
            'items': [i['sku'] for i in order['items']],
            'isConsumerFlowComplete': order['consumerStatuses']['isConsumerFlowComplete'],
        })
    return orders


def model_from_items(items):
    """Best-effort vehicle model (e.g. 'R1S', 'R2') from an order's items.

    Read from the vehicle item's configuration ruleset, used as a fallback when
    Rivian leaves the nested `order.vehicle` object null.
    """
    for i in items or []:
        config = i.get('configuration') or {}
        meta = (config.get('ruleset') or {}).get('meta') or {}
        if meta.get('vehicle'):
            return meta['vehicle']
    return None


def order_details(order_id, verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.order(order_id=order_id)
    except Exception:
        if verbose:
            print(f"Error getting order details for {order_id}")
        return {}
    if verbose:
        dump_response("order_details", response_json)
    data = {}
    order = (response_json.get('data') or {}).get('order') \
        if isinstance(response_json, dict) else None
    if not order:
        return data
    if order.get('vehicle'):
        try:
            data = {
                'vehicleId': order['vehicle']['vehicleId'],
                'vin': order['vehicle']['vin'],
                'modelYear': order['vehicle']['modelYear'],
                'make': order['vehicle']['make'],
                'model': order['vehicle']['model'],
            }
        except Exception:
            log.warning(f"Order details missing key items, "
                        f"found: {order['vehicle']}")
    else:
        # Rivian does not always populate the nested `vehicle` object (e.g. for
        # some delivered orders, or vehicles not yet built). Recover what is
        # actually present elsewhere in the same response rather than dropping
        # it. vehicleId is only in the nested object, so it stays absent here
        # (handled downstream via user_information()).
        if order.get('vin'):
            data['vin'] = order['vin']
        model = model_from_items(order.get('items'))
        if model:
            data['model'] = model
    for i in order.get('items') or []:
        if i.get('configuration') is not None:
            for c in i['configuration']['options']:
                data[c['groupName']] = c['optionName']
    return data


def retail_orders(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.retail_orders()
    except Exception:
        if verbose:
            print("Error getting retail orders")
        return []
    if verbose:
        dump_response("retail_orders", response_json)
    orders = []
    data = response_json.get('data') if isinstance(response_json, dict) else None
    search_orders = (data or {}).get('searchOrders') or {}
    for order in search_orders.get('data') or []:
        orders.append({
            'id': order['id'],
            'orderDate': order['orderDate'],
            'state': order['state'],
            'fulfillmentSummaryStatus': order['fulfillmentSummaryStatus'],
            'items': [item['title'] for item in order['items']]
        })
    return orders


def get_order(order_id, verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_order(order_id=order_id)
    except Exception:
        if verbose:
            print(f"Error getting order info for {order_id}")
        return {}
    if verbose:
        dump_response("get_order", response_json)
    result = {}
    order = (response_json.get('data') or {}).get('order') \
        if isinstance(response_json, dict) else None
    if not order:
        return result
    result['orderDate'] = order.get('orderDate')
    result['fulfillmentSummaryStatus'] = order.get('fulfillmentSummaryStatus')
    # Most recent successful payment date — reflects recent order activity
    # (e.g. configuring/finalizing a long-standing reservation), unlike
    # orderDate which is the original order/reservation creation date.
    payment_dates = [p['date'] for p in (order.get('payments') or [])
                     if p.get('date')]
    result['lastPaymentDate'] = max(payment_dates) if payment_dates else None
    fulfillments = []
    for f in ((order.get('fulfillmentInfo') or {}).get('fulfillments') or []):
        fulfillments.append({
            'status': f.get('fulfillmentStatus'),
            'method': f.get('fulfillmentMethod'),
            'tracking': f.get('tracking'),
            'estimatedDeliveryWindow': f.get('estimatedDeliveryWindow'),
        })
    result['fulfillments'] = fulfillments
    return result


def print_order_fulfillment(order_id, verbose, privacy):
    """Print carrier tracking and estimated delivery window for an order's
    fulfillments, when present (e.g. shipped accessories / retail items).

    Vehicles are delivered by appointment, so their fulfillments carry no
    tracking; this stays silent in that case.
    """
    order_info = get_order(order_id, verbose)
    last_payment = order_info.get('lastPaymentDate')
    if last_payment:
        print(f"Last payment: {last_payment[:10]}")
    for f in order_info.get('fulfillments') or []:
        edw = f.get('estimatedDeliveryWindow') or {}
        if edw.get('startDate') or edw.get('endDate'):
            print(f"Estimated delivery window: "
                  f"{edw.get('startDate')} - {edw.get('endDate')}")
        tracking = f.get('tracking') or {}
        if tracking.get('number'):
            number = tracking['number']
            if privacy:
                number = 'xxxx' + number[-4:]
            print(f"Tracking ({f.get('status') or tracking.get('status')}):")
            if tracking.get('carrier'):
                print(f"   Carrier: {tracking['carrier']}")
            print(f"   Number: {number}")
            if tracking.get('shipDate'):
                print(f"   Shipped: {tracking['shipDate']}")
            if tracking.get('deliveredDate'):
                print(f"   Delivered: {tracking['deliveredDate']}")
            if tracking.get('url') and not privacy:
                print(f"   URL: {tracking['url']}")


def payment_methods(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.payment_methods()
    except Exception:
        if verbose:
            print("Error getting payment methods")
        return []
    if verbose:
        dump_response("payment_methods", response_json)
    pmt = []
    for p in gql_data(response_json, 'paymentMethods', default=[]) or []:
        pmt.append({
            'type': p['type'],
            'default': p['default'],
            'card': p['card'] if 'card' in p else None,
        })
    return pmt


def check_by_rivian_id(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.check_by_rivian_id()
    except Exception:
        if verbose:
            print("Error getting check_by_rivian_id")
        return {}
    if verbose:
        dump_response("check_by_rivian_id", response_json)
    return {'Chargepoint checkByRivianId':
            gql_data(response_json, 'chargepoint', 'checkByRivianId')}


def get_linked_email_for_rivian_id(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_linked_email_for_rivian_id()
    except Exception:
        if verbose:
            print("Error getting linked email for rivian id")
        return {}
    if verbose:
        dump_response("get_linked_email_for_rivian_id", response_json)
    return {
        'Chargepoint linked email':
            gql_data(response_json, 'chargepoint', 'getLinkedEmailForRivianId', 'email')
    }


def get_vehicle(vehicle_id, verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_vehicle(vehicle_id=vehicle_id)
    except Exception:
        if verbose:
            print(f"Error getting vehicle for {vehicle_id}")
        return []
    if verbose:
        dump_response("get_vehicle", response_json)
    data = []
    for u in gql_data(response_json, 'getVehicle', 'invitedUsers', default=[]) or []:
        if u['__typename'] != 'ProvisionedUser':
            continue
        ud = {
            'firstName': u['firstName'],
            'lastName': u['lastName'],
            'email': u['email'],
            'roles': ', '.join(u['roles']),
            'devices': [],
        }
        for d in u['devices']:
            ud['devices'].append({
                "id": d["id"],
                "deviceName": d["deviceName"],
                "isPaired": d["isPaired"],
                "isEnabled": d["isEnabled"],
            })
        data.append(ud)
    return data


def get_vehicle_state(vehicle_id, verbose, minimal=False):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_vehicle_state(vehicle_id=vehicle_id, minimal=minimal)
    except Exception as e:
        print(f"Error: {str(e)}")
        return None
    if verbose:
        dump_response("get_vehicle_state", response_json)
    if 'data' in response_json and 'vehicleState' in response_json['data']:
        return response_json['data']['vehicleState']
    else:
        return None


def get_vehicle_last_seen(vehicle_id, verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_vehicle_last_connection(vehicle_id=vehicle_id)
    except Exception as e:
        print(f"{str(e)}")
        return None
    if verbose:
        dump_response("get_vehicle_last_seen", response_json)
    last_sync = gql_data(response_json, 'vehicleState', 'cloudConnection', 'lastSync')
    return parse(last_sync) if last_sync else None


def plan_trip(vehicle_id, starting_soc, starting_range_meters, origin_lat, origin_long, dest_lat, dest_long, verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.plan_trip(
            vehicle_id=vehicle_id,
            starting_soc=float(starting_soc),
            starting_range_meters=float(starting_range_meters),
            origin_lat=float(origin_lat),
            origin_long=float(origin_long),
            dest_lat=float(dest_lat),
            dest_long=float(dest_long),
        )
    except Exception as e:
        print(f"{str(e)}")
        return None
    if verbose:
        dump_response("plan_trip", response_json)
    return response_json


def get_ota_info(vehicle_id, verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_ota_details(vehicle_id=vehicle_id)
    except Exception as e:
        print(f"{str(e)}")
        return None
    if verbose:
        dump_response("get_ota_info", response_json)
    return gql_data(response_json, 'getVehicle', default={}) or {}


def transaction_status(order_id, verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.transaction_status(order_id)
    except Exception as e:
        if verbose:
            print(f"Error getting transaction status for {order_id}")
        return None
    if verbose:
        dump_response("transaction_status", response_json)
    status = {}
    if response_json and \
            'data' in response_json and \
            response_json['data'] and \
            "transactionStatus" in response_json['data']:
        transaction_status = response_json['data']['transactionStatus']
        for s in (
            'titleAndReg',
            'tradeIn',
            'finance',
            'delivery',
            'insurance',
            'documentUpload',
            'contracts',
            'payment',
        ):
            status[transaction_status[s]['consumerStatus']['displayOrder']] = {
                'item': s,
                'status': transaction_status[s]['sourceStatus']['status'],
                'complete': transaction_status[s]['consumerStatus']['complete']
            }
    return status


def chargers(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_registered_wallboxes()
    except Exception:
        if verbose:
            print("Error getting chargers")
        return []
    if verbose:
        dump_response("chargers", response_json)
    return gql_data(response_json, 'getRegisteredWallboxes', default=[]) or []


def delivery(order_id, verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.delivery(order_id=order_id)
    except Exception:
        if verbose:
            print(f"Error getting delivery info for {order_id}")
        return {}
    if verbose:
        dump_response("delivery", response_json)
    vehicle = {}
    data = response_json.get('data') if isinstance(response_json, dict) else None
    delivery_info = data.get('delivery') if data else None
    if delivery_info:
        vehicle['vin'] = delivery_info['vehicleVIN']
        vehicle['carrier'] = delivery_info['carrier']
        vehicle['status'] = delivery_info['status']
        vehicle['appointmentDetails'] = delivery_info['appointmentDetails']
    return vehicle


def speakers(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_provisioned_camp_speakers()
    except Exception:
        if verbose:
            print("Error getting speakers")
        return []
    if verbose:
        dump_response("speakers", response_json)
    return gql_data(response_json, 'currentUser', 'vehicles', default=[]) or []


def images(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_vehicle_images()
    except Exception:
        if verbose:
            print("Error getting images")
        return []
    if verbose:
        dump_response("images", response_json)
    images = []
    for i in gql_data(response_json, 'getVehicleOrderMobileImages', default=[]) or []:
        images.append({
            'size': i['size'],
            'design': i['design'],
            'placement': i['placement'],
            'url': i['url']
        })
    return images


def get_user(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.user()
    except Exception:
        if verbose:
            print("Error getting user details")
        return {}
    if verbose:
        dump_response("get_user", response_json)
    u = gql_data(response_json, 'user', default={}) or {}
    if not u:
        return {}
    user = {
        'userId': u.get('userId'),
        'email': (u.get('email') or {}).get('email'),
        'phone': (u.get('phone') or {}).get('formatted'),
        'firstName': u.get('firstName'),
        'lastName': u.get('lastName'),
        'newsletterSubscription': u.get('newsletterSubscription'),
        'smsSubscription': u.get('smsSubscription'),
        'registrationChannels2FA': u.get('registrationChannels2FA') or {},
        'addresses': [],
    }
    for a in u.get('addresses') or []:
        user['addresses'].append({
            'type': a['type'],
            'line1': a['line1'],
            'line2': a['line2'],
            'city': a['city'],
            'state': a['state'],
            'country': a['country'],
            'postalCode': a['postalCode'],
        })
    return user


def charging_schedule(vehicle_id, verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_charging_schedule(vehicle_id)
    except Exception:
        if verbose:
            print("Error getting charging schedule")
        return []
    if verbose:
        dump_response("get_charging_schedule", response_json)
    return gql_data(response_json, 'getVehicle', 'chargingSchedules', default=[]) or []


def charging_sessions(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_completed_session_summaries()
    except Exception:
        if verbose:
            print("Error getting charging sessions")
        return []
    if verbose:
        dump_response("get_completed_session_summaries", response_json)
    sessions = []
    for s in gql_data(response_json, 'getCompletedSessionSummaries', default=[]) or []:
        sessions.append({
            'charge_start': s['startInstant'],
            'charge_end': s['endInstant'],
            'energy': s['totalEnergyKwh'],
            'vendor': s['vendor'],
            'range_added': s['rangeAddedKm'],
            'transaction_id': s['transactionId'],
        })
    # sort sessions by charge_start
    sessions.sort(key=lambda x: x['charge_start'])
    return sessions


def charging_session(verbose):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_non_rivian_user_session()
    except Exception:
        if verbose:
            print("Error getting charging session")
        return None
    if verbose:
        dump_response("get_non_rivian_user_session", response_json)
    return gql_data(response_json, 'getNonRivianUserSession')


def live_charging_session(vehicle_id, verbose=False):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_live_session_data(vehicle_id)
    except Exception:
        if verbose:
            print("Error getting live charging session")
        return None
    if verbose:
        dump_response("get_live_session_data", response_json)
    return gql_data(response_json, 'getLiveSessionData')


def live_charging_history(vehicle_id, verbose=False):
    rivian = get_rivian_object()
    try:
        response_json = rivian.get_live_session_history(vehicle_id)
    except Exception:
        if verbose:
            print("Error getting live charging history")
        return []
    if verbose:
        dump_response("get_live_session_history", response_json)
    history = gql_data(response_json, 'getLiveSessionHistory', 'chartData', default=[]) or []
    # sort history by 'time'
    history.sort(key=lambda x: x['time'])
    return history


def vehicle_command(command, vehicle_id=None, verbose=False):
    vehiclePublicKey = None
    user_info = user_information(verbose)
    for v in user_info.get('vehicles', []):
        if vehicle_id and v['id'] == vehicle_id:
            found = True
        else:
            vehicle_id = v['id']
            found = True
        if found:
            vehiclePublicKey = v['vas']['vehiclePublicKey']
            break
        # Only need first
    enrolled_phones = user_info.get('enrolledPhones') or []
    if not enrolled_phones:
        print("No enrolled phones found for this account")
        return
    vasPhoneId = enrolled_phones[0]['vas']['vasPhoneId']
    deviceName = enrolled_phones[0]['enrolled'][0]['deviceName']

    vehicle = get_vehicle(vehicle_id=vehicle_id, verbose=verbose)
    deviceId = None
    for u in vehicle:
        for d in u['devices']:
            if d['deviceName'] == deviceName:
                deviceId = d['id']
                break
        if deviceId:
            break

    print(f"Vehicle ID: {vehicle_id} vasPhoneID: {vasPhoneId} vehiclePublicKey: {vehiclePublicKey} deviceId: {deviceId}")


def test_graphql(verbose):
    rivian = get_rivian_object()
    query = {
        "operationName": "GetAdventureFeed",
        "query": 'query GetAdventureFeed($locale: String!, $slug: String!) { egAdventureFeedCollection(locale: $locale, limit: 1, where: { slug: $slug } ) { items { slug entryTitle cardsCollection(limit: 15) { items { __typename ... on EgAdventureFeedStoryCard { slug entryTitle title subtitle cover { entryTitle sourcesCollection(limit: 1) { items { entryTitle media auxiliaryData { __typename ... on EgImageAuxiliaryData { altText } } } } } slidesCollection { items { entryTitle duration theme gradient mediaCollection(limit: 2) { items { __typename ... on EgCloudinaryMedia { entryTitle sourcesCollection(limit: 1) { items { entryTitle media auxiliaryData { __typename ... on EgImageAuxiliaryData { altText } } } } } ... on EgLottieAnimation { entryTitle altText media mode } } } } } } ... on EgAdventureFeedEditorialCard { slug entryTitle title subtitle cover { entryTitle sourcesCollection(limit: 1) { items { entryTitle media auxiliaryData { __typename ... on EgImageAuxiliaryData { altText } } } } } sectionsCollection { items { entryTitle theme mediaCollection(limit: 2) { items { __typename ... on EgCloudinaryMedia { entryTitle sourcesCollection(limit: 1) { items { entryTitle media auxiliaryData { __typename ... on EgImageAuxiliaryData { altText } } } } } ... on EgLottieAnimation { entryTitle altText media mode } } } } } } } } } } }',
        "variables": {
            "locale": "en_US",
        },
    }
    response = rivian.raw_graphql_query(url=RIVIAN_CONTENT_PATH, query=query, headers=rivian.gateway_headers())
    response_json = response.json()
    if verbose:
        dump_response("test_graphql", response_json)


def get_local_time(ts):
    if type(ts) is str:
        try:
            t = parse(ts)
        except:
            return
    else:
        t = ts
    to_zone = tz.tzlocal()
    if t:
        t = t.astimezone(to_zone)
    return t


def show_local_time(ts):
    t = get_local_time(ts)
    return t.strftime("%m/%d/%Y, %H:%M%p %Z") if t else None


def celsius_to_temp_units(c, metric=False):
    if metric:
        return c
    else:
        return (c * 9/5) + 32


def meters_to_distance_units(m, metric=False):
    if metric:
        return m / 1000
    else:
        return m / 1609.0


def miles_to_meters(m, metric=False):
    if metric:
        return m
    else:
        return m * 1609.0


def kilometers_to_distance_units(m, metric=False):
    if metric:
        return m
    else:
        return (m * 1000) / 1609.0


def get_elapsed_time_string(elapsed_time_in_seconds):
    elapsed_time = timedelta(seconds=elapsed_time_in_seconds)
    total_seconds = int(elapsed_time.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours} hours, {minutes} minutes, {seconds} seconds"


def main():
    parser = argparse.ArgumentParser(description='Rivian CLI')
    parser.add_argument('--login', help='Login to account', required=False, action='store_true')
    parser.add_argument('--user', help='Display user info', required=False, action='store_true')
    parser.add_argument('--vehicles', help='Display vehicles', required=False, action='store_true')
    parser.add_argument('--chargers', help='Display chargers', required=False, action='store_true')
    parser.add_argument('--speakers', help='Display Speakers', required=False, action='store_true')
    parser.add_argument('--images', help='Display Image URLs', required=False, action='store_true')
    parser.add_argument('--vehicle_orders', help='Display vehicle orders', required=False, action='store_true')
    parser.add_argument('--retail_orders', help='Display retail orders', required=False, action='store_true')
    parser.add_argument('--payment_methods', help='Show payment methods', required=False, action='store_true')
    parser.add_argument('--test', help='For testing graphql queries', required=False, action='store_true')
    parser.add_argument('--charge_ids', help='Show charge_ids', required=False, action='store_true')
    parser.add_argument('--verbose', help='Verbose output', required=False, action='store_true')
    parser.add_argument('--raw', help='Dump raw JSON API responses (for field discovery)', required=False, action='store_true')
    parser.add_argument('--privacy', help='Fuzz order/vin info', required=False, action='store_true')
    parser.add_argument('--state', help='Get vehicle state', required=False, action='store_true')
    parser.add_argument('--vehicle', help='Get vehicle access info', required=False, action='store_true')
    parser.add_argument('--vehicle_id', help='Vehicle to query (defaults to first one found)', required=False)
    parser.add_argument('--last_seen', help='Timestamp vehicle was last seen', required=False, action='store_true')
    parser.add_argument('--user_info', help='Show user information', required=False, action='store_true')
    parser.add_argument('--ota', help='Show user information', required=False, action='store_true')
    parser.add_argument('--poll', help='Poll vehicle state', required=False, action='store_true')
    parser.add_argument('--poll_frequency', help='Poll frequency', required=False, default=30, type=int)
    parser.add_argument('--poll_show_all', help='Show all poll results even if no changes occurred', required=False, action='store_true')
    parser.add_argument('--poll_inactivity_wait',
                        help='If not sleeping and nothing changes for this period of time '
                             'then do a poll_sleep_wait. Defaults to 0 for continual polling '
                             'at poll_frequency',
                        required=False, default=0, type=int)
    parser.add_argument('--poll_sleep_wait',
                        help='# How long to stop polling to let car go to sleep (depends on poll_inactivity_wait)',
                        required=False, default=40*60, type=int)
    parser.add_argument('--query', help='Single poll instance (quick poll)', required=False, action='store_true')
    parser.add_argument('--metric', help='Use metric vs imperial units', required=False, action='store_true')
    parser.add_argument('--plan_trip', help='Plan a trip - starting soc, starting range in meters, origin lat,origin long,dest lat,dest long', required=False)

    parser.add_argument('--charging_schedule', help='Get charging schedule', required=False, action='store_true')
    parser.add_argument('--charge_sessions', help='Get charging sessions', required=False, action='store_true')
    parser.add_argument('--last_charge', help='Get last charge session', required=False, action='store_true')
    parser.add_argument('--charge_session', help='Get current charging session', required=False, action='store_true')
    parser.add_argument('--live_charging_session', help='Get live charging session', required=False, action='store_true')
    parser.add_argument('--live_charging_history', help='Get live charging session history', required=False, action='store_true')

    parser.add_argument('--all', help='Run all commands silently as a sort of test of all commands', required=False, action='store_true')
    parser.add_argument('--command', help='Send vehicle a command', required=False,
                        choices=['WAKE_VEHICLE',
                                 'OPEN_FRUNK',
                                 'CLOSE_FRUNK',
                                 'OPEN_ALL_WINDOWS',
                                 'CLOSE_ALL_WINDOWS',
                                 'UNLOCK_ALL_CLOSURES',
                                 'LOCK_ALL_CLOSURES',
                                 'ENABLE_GEAR_GUARD_VIDEO',
                                 'DISABLE_GEAR_GUARD_VIDEO',
                                 'HONK_AND_FLASH_LIGHTS',
                                 'OPEN_TONNEAU_COVER',
                                 'CLOSE_TONNEAU_COVER',
                                 ]
                        )
    args = parser.parse_args()
    original_stdout = sys.stdout

    # --raw implies the raw-dump path: turn on the per-command response dumps
    # and switch their format to pretty JSON.
    if args.raw:
        global RAW
        RAW = True
        args.verbose = True

    if args.all:
        print("Running all commands silently")
        f = open(os.devnull, 'w')
        sys.stdout = f

    if args.login:
        login(args.verbose)

    rivian_info = {
        'vehicle_orders': [],
        'retail_orders': [],
        'vehicles': [],
    }

    if args.metric:
        distance_units = "km"
        distance_units_string = "kph"
        temp_units_string = "C"
    else:
        distance_units = "mi"
        distance_units_string = "mph"
        temp_units_string = "F"

    vehicle_id = None
    if args.vehicle_id:
        vehicle_id = args.vehicle_id

    needs_vehicle = args.vehicles or \
                    args.vehicle or \
                    args.state or \
                    args.last_seen or \
                    args.ota or \
                    args.poll or \
                    args.query or \
                    args.plan_trip or \
                    args.user_info or \
                    args.charge_session or \
                    args.live_charging_session or \
                    args.live_charging_history or \
                    args.charging_schedule or \
                    args.all

    if args.vehicle_orders or (needs_vehicle and not args.vehicle_id):
        verbose = args.vehicle_orders and args.verbose
        rivian_info['vehicle_orders'] = vehicle_orders(verbose)

    if args.vehicle_orders or args.all:
        if len(rivian_info['vehicle_orders']):
            print("Vehicle Orders:")
            for order in rivian_info['vehicle_orders']:
                order_id = 'xxxx' + order['id'][-4:] if args.privacy else order['id']
                print(f"Order ID: {order_id}")
                print(f"Order Date: {order['orderDate'][:10] if args.privacy else order['orderDate']}")
                print(f"Config State: {order['configurationStatus']}")
                print(f"Order State: {order['state']}")
                print(f"Status: {order['fulfillmentSummaryStatus']}")
                print(f"Item: {order['items'][0]}")
                print(f"Customer flow complete: {'Yes' if order['isConsumerFlowComplete'] else 'No'}")

                # Get delivery info
                delivery_status = delivery(order['id'], args.verbose)
                if 'carrier' in delivery_status:
                    print(f"Delivery carrier: {delivery_status['carrier']}")
                if 'status' in delivery_status:
                    print(f"Delivery status: {delivery_status['status']}")
                if 'appointmentDetails' in delivery_status and delivery_status['appointmentDetails']:
                    print("Delivery appointment details:")
                    start = parse(delivery_status['appointmentDetails']['startDateTime'])
                    end = parse(delivery_status['appointmentDetails']['endDateTime'])
                    print(f'   Start: {start.strftime("%m/%d/%Y, %H:%M %p")}')
                    print(f'   End  : {end.strftime("%m/%d/%Y, %H:%M %p")}')
                else:
                    print("Delivery appointment details: Not available yet")

                # Get fulfillment tracking / estimated delivery window
                print_order_fulfillment(order['id'], args.verbose, args.privacy)

                # Get transaction steps
                if order['id']:
                    transaction_steps = None
                    try:
                        transaction_steps = transaction_status(order['id'], args.verbose)
                    except Exception as e:
                        if verbose:
                            print(f"Error getting transaction status for {order_id}")
                    i = 1
                    completed = 0
                    if transaction_steps:
                        for s in transaction_steps:
                            if transaction_steps[s]['complete']:
                                completed += 1
                        print(f"{completed}/{len(transaction_steps)} Steps Complete:")
                        for s in sorted(transaction_steps):
                            print(f"   Step: {s}: {transaction_steps[s]['item']}: {transaction_steps[s]['status']}, Complete: {transaction_steps[s]['complete']}")
                            i += 1

                print("\n")
        else:
            print("No Vehicle Orders found")

    if args.retail_orders or args.all:
        rivian_info['retail_orders'] = retail_orders(args.verbose)
        if len(rivian_info['retail_orders']):
            print("Retail Orders:")
            for order in rivian_info['retail_orders']:
                order_id = 'xxxx' + order['id'][-4:] if args.privacy else order['id']
                print(f"Order ID: {order_id}")
                print(f"Order Date: {order['orderDate'][:10] if args.privacy else order['orderDate']}")
                print(f"Order State: {order['state']}")
                print(f"Status: {order['fulfillmentSummaryStatus']}")
                print(f"Items: {', '.join(order['items'])}")
                print_order_fulfillment(order['id'], args.verbose, args.privacy)
                print("\n")
        else:
            print("No Retail Orders found")

    if args.vehicles or args.all or (needs_vehicle and not args.vehicle_id):
        found_vehicle = False
        verbose = args.vehicles and args.verbose
        for order in rivian_info['vehicle_orders']:
            details = order_details(order['id'], verbose)
            vehicle = {}
            for i in details:
                value = details[i]
                vehicle[i] = value
            rivian_info['vehicles'].append(vehicle)
            if not found_vehicle:
                if args.vehicle_id:
                    if vehicle['vehicleId'] == args.vehicle_id:
                        found_vehicle = True
                elif 'vehicleId' in vehicle:
                    vehicle_id = vehicle['vehicleId']
                    found_vehicle = True
        if not found_vehicle:
            user_info = user_information(verbose)
            for v in user_info.get('vehicles', []):
                if 'id' in v:
                    vehicle_id = v['id']
                    found_vehicle = True
                    break
        if not found_vehicle:
            print(f"Didn't find vehicle ID {args.vehicle_id}")
            return -1

    if args.vehicles or args.all:
        if len(rivian_info['vehicles']):
            print("Vehicles:")
            for v in rivian_info['vehicles']:
                for i in v:
                    print(f"{i}: {v[i]}")
                print("\n")
        else:
            print("No Vehicles found")

    if args.payment_methods or args.all:
        pmt = payment_methods(args.verbose)
        print("Payment Methods:")
        if len(pmt):
            for p in pmt:
                print(f"Type: {p['type']}")
                print(f"Default: {p['default']}")
                if p['card']:
                    for i in p['card']:
                        print(f"Card {i}: {p['card'][i]}")
                print("\n")
        else:
            print("No Payment Methods found")

    if args.charge_ids or args.all:
        print("Charge IDs:")
        data = check_by_rivian_id(args.verbose)
        for i in data:
            print(f"{i}: {data[i]}")
        data = get_linked_email_for_rivian_id(args.verbose)
        for i in data:
            print(f"{i}: {data[i]}")
        print("\n")

    # For testing new graphql queries
    if args.test:
        test_graphql(args.verbose)

    if args.chargers or args.all:
        rivian_info['chargers'] = chargers(args.verbose)
        if len(rivian_info['chargers']):
            print("Chargers:")
            for c in rivian_info['chargers']:
                for i in c:
                    print(f"{i}: {c[i]}")
                print("\n")
        else:
            print("No Chargers found")

    if args.speakers or args.all:
        rivian_info['speakers'] = speakers(args.verbose)
        if len(rivian_info['speakers']):
            print("Speakers:")
            for v in rivian_info['speakers']:
                print(f"Vehicle ID: {v['id']}")
                for c in v['connectedProducts']:
                    print(f"   {c['__typename']}: Serial # {c['serialNumber']}")
        else:
            print("No Speakers found")

    if args.ota or args.all:
        ota = get_ota_info(vehicle_id, args.verbose)
        if len(ota):
            if ota['availableOTAUpdateDetails']:
                print(f"Available OTA Version: {ota['availableOTAUpdateDetails']['version']}")
                print(f"Available OTA Release notes: {ota['availableOTAUpdateDetails']['url']}")
            if ota['currentOTAUpdateDetails']:
                print(f"Current Version: {ota['currentOTAUpdateDetails']['version']}")
                print(f"Current Version Release notes: {ota['currentOTAUpdateDetails']['url']}")
        else:
            print("No OTA info available")

    # Basic images for vehicle
    if args.images or args.all:
        rivian_info['images'] = images(args.verbose)
        if len(rivian_info['images']):
            print("Images:")
            for c in rivian_info['images']:
                for i in c:
                    print(f"{i}: {c[i]}")
                print("\n")
        else:
            print("No Images found")

    if args.user_info or args.all:
        print("User Vehicles:")
        user_info = user_information(args.verbose)
        for v in user_info.get('vehicles', []):
            print(f"Vehicle ID: {v['id']}")
            if args.privacy:
                vin = v['vin'][-8:-3] + 'xxx'
            else:
                vin = v['vin']
            print(f"   Vin: {vin}")
            print(f"   State: {v['state']}")
            print(f"   Kind: {v['vehicle']['modelYear']} {v['vehicle']['make']} {v['vehicle']['model']}")
            print(f"   General assembly date: {v['vehicle']['actualGeneralAssemblyDate']}")
            print(f"   OTA early access: {v['vehicle']['otaEarlyAccessStatus']}")
            if ('vehicleState' in v['vehicle'] and v['vehicle']['vehicleState'] and
                    'supportedFeatures' in v['vehicle']['vehicleState']):
                print("   Features:")
                for f in v['vehicle']['vehicleState']['supportedFeatures']:
                    print(f"      {f['name']}: {f['status']}")
        for p in user_info.get('enrolledPhones', []):
            print("Enrolled phones:")
            for d in p['enrolled']:
                if d['vehicleId'] == vehicle_id:
                    print(f"   Device Name: {d['deviceName']}")
                    print(f"   Device identityId: {d['identityId']}")
            print(f"   vasPhoneId: {p['vas']['vasPhoneId']}")
            print(f"   publicKey: {p['vas']['publicKey']}")

    if (args.user or args.all) and not args.privacy:
        user = get_user(args.verbose)
        print("User details:")
        for i in user:
            if i == 'registrationChannels2FA':
                for j in user[i]:
                    print(f"registrationChannels2FA {j}: {user[i][j]}")
            elif i == 'addresses':
                address_num = 1
                for a in user[i]:
                    print(f"Address {address_num}:")
                    for j in a:
                        data = a[j]
                        if type(data) == list:
                            data = ", ".join(data)
                        print(f"   {j}: {data}")
                    address_num += 1
            else:
                print(f"{i}: {user[i]}")
        print("\n")

    if args.state or args.all:
        state = get_vehicle_state(vehicle_id, args.verbose)
        if not state:
            print("Unable to retrieve vehicle state, try with --verbose")
        else:
            print("Vehicle State:")
            print(f"Power State: {state['powerState']['value']}")
            print(f"Drive Mode: {state['driveMode']['value']}")
            print(f"Gear Status: {state['gearStatus']['value']}")
            print(f"Odometer: {meters_to_distance_units(state['vehicleMileage']['value'], args.metric):.1f} {distance_units}")
            if not args.privacy:
                print(f"Location: {state['gnssLocation']['latitude']},{state['gnssLocation']['longitude']}")
            print(f"Speed: {meters_to_distance_units(state['gnssSpeed']['value'], args.metric):.1f} {distance_units}/h")
            print(f"Bearing: {state['gnssBearing']['value']:.1f} degrees")
            print(f"Altitude: {state['gnssAltitude']['value']}")
            print(f"Location Error:")
            print(f"   Vertical {state['gnssError']['positionVertical']} m")
            print(f"   Horizontal {state['gnssError']['positionHorizontal']} m")
            print(f"   Speed {meters_to_distance_units(state['gnssError']['speed'], args.metric):.1f} {distance_units}/h")
            print(f"   Bearing {state['gnssError']['bearing']} degrees")

            print("Battery:")
            print(f"   Battery Level: {state['batteryLevel']['value']:.1f}%")
            print(f"   Range: {kilometers_to_distance_units(state['distanceToEmpty']['value'], args.metric):.1f} {distance_units}")
            print(f"   Battery Limit: {state['batteryLimit']['value']:.1f}%")
            print(f"   Battery Capacity: {state['batteryCapacity']['value']} kW")
            print(f"   Charging state: {state['chargerState']['value']}")
            if state['chargerStatus']:
                print(f"   Charger status: {state['chargerStatus']['value']}")
            print(f"   Time to end of charge: {state['timeToEndOfCharge']['value']}")
            print(f"   Charging Time Estimation Validity: {state['chargingTimeEstimationValidity']['value']}")
            print(f"   Limited Accel Cold: {state['limitedAccelCold']['value']}")
            print(f"   Limited Regen Cold: {state['limitedRegenCold']['value']}")
            if state.get('rangeThreshold'):
                print(f"   Range Threshold: {state['rangeThreshold']['value']}")
            if state.get('chargerDerateStatus'):
                print(f"   Charger Derate: {state['chargerDerateStatus']['value']}")
            if state.get('remoteChargingAvailable'):
                print(f"   Remote Charging Available: {bool(state['remoteChargingAvailable']['value'])}")
            if state.get('batteryHvThermalEvent'):
                print(f"   HV Thermal Event: {state['batteryHvThermalEvent']['value']}")
            if state.get('batteryHvThermalEventPropagation'):
                print(f"   HV Thermal Propagation: {state['batteryHvThermalEventPropagation']['value']}")


            print("OTA:")
            print(f"   Current Version: {state['otaCurrentVersion']['value']}")
            print(f"   Available version: {state['otaAvailableVersion']['value']}")
            if state['otaStatus']:
                print(f"   Status: {state['otaStatus']['value']}")
            if state['otaInstallType']:
                print(f"   Install type: {state['otaInstallType']['value']}")
            if state['otaInstallDuration']:
                print(f"   Duration: {state['otaInstallDuration']['value']}")
            if state['otaDownloadProgress']:
                print(f"   Download progress: {state['otaDownloadProgress']['value']}")
            print(f"   Install ready: {state['otaInstallReady']['value']}")
            if state['otaInstallProgress']:
                print(f"   Install progress: {state['otaInstallProgress']['value']}")
            if state['otaInstallTime']:
                print(f"   Install time: {state['otaInstallTime']['value']}")
            if state['otaCurrentStatus']:
                print(f"   Current Status: {state['otaCurrentStatus']['value']}")

            print("Climate:")
            print(f"   Climate Interior Temp: {celsius_to_temp_units(state['cabinClimateInteriorTemperature']['value'], args.metric)}º{temp_units_string}")
            print(f"   Climate Driver Temp: {celsius_to_temp_units(state['cabinClimateDriverTemperature']['value'], args.metric)}º{temp_units_string}")
            print(f"   Cabin Preconditioning Status: {state['cabinPreconditioningStatus']['value']}")
            print(f"   Cabin Preconditioning Type: {state['cabinPreconditioningType']['value']}")
            print(f"   Defrost: {state['defrostDefogStatus']['value']}")
            print(f"   Steering Wheel Heat: {state['steeringWheelHeat']['value']}")
            print(f"   Pet Mode: {state['petModeStatus']['value']}")

            print("Security:")
            if state['alarmSoundStatus']:
                print(f"   Alarm active: {state['alarmSoundStatus']['value']}")
            if state['gearGuardVideoStatus']:
                print(f"   Gear Guard Video: {state['gearGuardVideoStatus']['value']}")
            if state['gearGuardVideoMode']:
                print(f"   Gear Guard Mode: {state['gearGuardVideoMode']['value']}")
            if state['alarmSoundStatus']:
                print(f"   Last Alarm: {show_local_time(state['alarmSoundStatus']['timeStamp'])}")
            print(f"   Gear Guard Locked: {state['gearGuardLocked']['value'] == 'locked'}")

            print(f"Charge Port: {state['chargePortState']['value']}")
            print("Doors:")
            print(f"   Front left locked: {state['doorFrontLeftLocked']['value'] == 'locked'}")
            print(f"   Front left closed: {state['doorFrontLeftClosed']['value'] == 'closed'}")
            print(f"   Front right locked: {state['doorFrontRightLocked']['value'] == 'locked'}")
            print(f"   Front right closed: {state['doorFrontRightClosed']['value'] == 'closed'}")
            print(f"   Rear left locked: {state['doorRearLeftLocked']['value'] == 'locked'}")
            print(f"   Rear left closed: {state['doorRearLeftClosed']['value'] == 'closed'}")
            print(f"   Rear right locked: {state['doorRearRightLocked']['value'] == 'locked'}")
            print(f"   Rear right closed: {state['doorRearRightClosed']['value'] == 'closed'}")

            print("Windows:")
            print(f"   Front left closed: {state['windowFrontLeftClosed']['value'] == 'closed'}")
            print(f"   Front right closed: {state['windowFrontRightClosed']['value'] == 'closed'}")
            print(f"   Rear left closed: {state['windowRearLeftClosed']['value'] == 'closed'}")
            print(f"   Rear right closed: {state['windowRearRightClosed']['value'] == 'closed'}")
            print(f"   Next Action: {state['windowsNextAction']['value']}")

            print("Seats:")
            print(f"   Front left Heat: {state['seatFrontLeftHeat']['value'] == 'On'}")
            print(f"   Front right Heat: {state['seatFrontRightHeat']['value'] == 'On'}")
            print(f"   Rear left Heat: {state['seatRearLeftHeat']['value'] == 'On'}")
            print(f"   Rear right Heat: {state['seatRearRightHeat']['value'] == 'On'}")
            if state.get('seatFrontLeftVent'):
                print(f"   Front left Vent: {state['seatFrontLeftVent']['value'] == 'On'}")
            if state.get('seatFrontRightVent'):
                print(f"   Front right Vent: {state['seatFrontRightVent']['value'] == 'On'}")

            print("Storage:")
            print("   Frunk:")
            print(f"      Frunk locked: {state['closureFrunkLocked']['value'] == 'locked'}")
            print(f"      Frunk closed: {state['closureFrunkClosed']['value'] == 'closed'}")
            print(f"      Frunk Next Action: {state['closureFrunkNextAction']['value']}")

            print("   Lift Gate:")
            print(f"      Lift Gate Locked: {state['closureLiftgateLocked']['value'] == 'locked'}")
            print(f"      Lift Gate Closed: {state['closureLiftgateClosed']['value']}")
            print(f"      Lift Next Action: {state['closureLiftgateNextAction']['value']}")

            print("   Tonneau:")
            print(f"      Tonneau Locked: {state['closureTonneauLocked']['value']}")
            print(f"      Tonneau Closed: {state['closureTonneauClosed']['value']}")

            print("Trailer:")
            print(f"   Trailer Status: {state['trailerStatus']['value']}")
            if state['rearHitchStatus']:
                print(f"   Rear Hitch Status: {state['rearHitchStatus']['value']}")

            print("Maintenance:")
            print(f"   Service Mode: {state['serviceMode']['value']}")
            print(f"   Car Wash Mode: {state['carWashMode']['value']}")
            print(f"   Wiper Fluid: {state['wiperFluidState']['value']}")
            if state.get('brakeFluidLow'):
                print(f"   Brake Fluid Low: {state['brakeFluidLow']['value']}")
            print("   Tire pressures:")
            print(f"      Front Left: {state['tirePressureStatusFrontLeft']['value']}")
            print(f"      Front Right: {state['tirePressureStatusFrontRight']['value']}")
            print(f"      Rear Left: {state['tirePressureStatusRearLeft']['value']}")
            print(f"      Rear Right: {state['tirePressureStatusRearRight']['value']}")
            print(f"   12V Battery: {state['twelveVoltBatteryHealth']['value']}")
            if state['btmFfHardwareFailureStatus']:
                print(f"   btmFf Hardware Failure Status {state['btmFfHardwareFailureStatus']['value']}")
            if state['btmIcHardwareFailureStatus']:
                print(f"   btmIc Hardware Failure Status {state['btmIcHardwareFailureStatus']['value']}")
            if state['btmLfdHardwareFailureStatus']:
                print(f"   btmLfd Hardware Failure Status {state['btmLfdHardwareFailureStatus']['value']}")
            if state['btmRfdHardwareFailureStatus']:
                print(f"   btmRfd Hardware Failure Status {state['btmRfdHardwareFailureStatus']['value']}")

    if args.poll or args.query or args.all:
        single_poll = args.query or args.all
        # Power state = ready, go, sleep, standby,
        # Charge State = charging_ready or charging_active
        # Charger Status = chrgr_sts_not_connected, chrgr_sts_connected_charging, chrgr_sts_connected_no_chrg
        if not single_poll:
            print(f"Polling car every {args.poll_frequency} seconds, only showing changes in data.")
            if args.poll_inactivity_wait:
                print(f"If 'ready' and inactive for {args.poll_inactivity_wait / 60:.0f} minutes will pause polling once for "
                      f"every ready state cycle for {args.poll_sleep_wait / 60:.0f} minutes to allow car to go to sleep.")
            print("")

        if args.privacy:
            lat_long_title = ''
        else:
            lat_long_title = 'Latitude,Longitude,'
        print(f"timestamp,Power,Drive Mode,Gear,Mileage,Battery,Range,Speed,{lat_long_title}Charger Status,Charge State,Battery Limit,Charge End")
        last_state_change = time.time()
        last_state = None
        last_power_state = None
        long_sleep_completed = False
        last_mileage = None
        distance_time = None
        elapsed_time = None
        speed = 0
        found_bad_response = False
        while True:
            state = get_vehicle_state(vehicle_id, args.verbose, minimal=True)
            if not state:
                if not found_bad_response:
                    print(f"{datetime.now().strftime('%m/%d/%Y, %H:%M:%S %p %Z').strip()} Rivian API appears offline")
                found_bad_response = True
                last_state = None
                if single_poll:
                    # One-shot (--query, e.g. telegraf): don't retry forever on
                    # an API outage; exit so the caller retries next interval.
                    break
                time.sleep(args.poll_frequency)
                continue
            found_bad_response = False
            if last_power_state != 'ready' and state['powerState']['value'] == 'ready':
                # Allow one long sleep per ready state cycle to allow car to sleep
                long_sleep_completed = False
            last_power_state = state['powerState']['value']
            if distance_time:
                elapsed_time = (datetime.now() - distance_time).total_seconds()
            if last_mileage and elapsed_time:
                distance_meters = state['vehicleMileage']['value'] - last_mileage
                distance = meters_to_distance_units(distance_meters, args.metric)
                speed = distance * (60 * 60 / elapsed_time)
            last_mileage = state['vehicleMileage']['value']
            distance_time = datetime.now()
            current_state = \
                f"{state['powerState']['value']}," \
                f"{state['driveMode']['value']}," \
                f"{state['gearStatus']['value']}," \
                f"{meters_to_distance_units(state['vehicleMileage']['value'], args.metric):.1f}," \
                f"{state['batteryLevel']['value']:.1f}%," \
                f"{kilometers_to_distance_units(state['distanceToEmpty']['value'], args.metric):.1f}," \
                f"{speed:.1f} {distance_units_string},"
            if not args.privacy:
                current_state += \
                    f"{state['gnssLocation']['latitude']}," \
                    f"{state['gnssLocation']['longitude']},"
            if state['chargerStatus']:
                current_state += \
                    f"{state['chargerStatus']['value']}," \
                    f"{state['chargerState']['value']}," \
                    f"{state['batteryLimit']['value']:.1f}%," \
                    f"{state['timeToEndOfCharge']['value'] // 60}h{state['timeToEndOfCharge']['value'] % 60}m"
            if args.poll_show_all or single_poll or current_state != last_state:
                print(f"{datetime.now().strftime('%m/%d/%Y, %H:%M:%S %p %Z').strip()}," + current_state)
                last_state_change = datetime.now()
            last_state = current_state
            if single_poll:
                break
            if state['powerState']['value'] == 'sleep':
                time.sleep(args.poll_frequency)
            else:
                delta = (datetime.now() - last_state_change).total_seconds()
                if args.poll_inactivity_wait and not long_sleep_completed and delta >= args.poll_inactivity_wait:
                    print(f"{datetime.now().strftime('%m/%d/%Y, %H:%M:%S %p %Z').strip()} "
                          f"Sleeping for {args.poll_sleep_wait / 60:.0f} minutes")
                    time.sleep(args.poll_sleep_wait)
                    print(f"{datetime.now().strftime('%m/%d/%Y, %H:%M:%S %p %Z').strip()} "
                          f"Back to polling every {args.poll_frequency} seconds, showing changes only")
                    long_sleep_completed = True
                else:
                    time.sleep(args.poll_frequency)

    if args.vehicle or args.all:
        vehicle = get_vehicle(vehicle_id, args.verbose)
        print("Vehicle Users:")
        for u in vehicle:
            print(f"{u['firstName']} {u['lastName']}")
            print(f"   Email: {u['email']}")
            print(f"   Roles: {u['roles']}")
            print("   Devices:")
            for d in u['devices']:
                print(f"      {d['deviceName']}, Paired: {d['isPaired']}, Enabled: {d['isEnabled']}, ID: {d['id']}")

    if args.last_seen or args.all:
        last_seen = get_vehicle_last_seen(vehicle_id, args.verbose)
        print(f"Vehicle last seen: {show_local_time(last_seen)}")

    if args.plan_trip or args.all:
        if args.all:
            starting_soc, starting_range, origin_lat, origin_long, dest_lat, dest_long = \
                ["85.0", "360", "42.0772", "-71.6303", "42.1399", "-71.5163"]
        else:
            if len(args.plan_trip.split(',')) == 4:
                starting_soc, starting_range, origin_place, dest_place = args.plan_trip.split(',')
                origin_lat, origin_long = extract_lat_long(origin_place)
                dest_lat, dest_long = extract_lat_long(dest_place)
            else:
                starting_soc, starting_range, origin_lat, origin_long, dest_lat, dest_long = args.plan_trip.split(',')

        starting_range_meters = miles_to_meters(float(starting_range), args.metric)
        planned_trip = plan_trip(
            vehicle_id,
            starting_soc,
            starting_range_meters,
            origin_lat,
            origin_long,
            dest_lat,
            dest_long,
            args.verbose
        )
        decode_and_map(planned_trip)

    if args.charging_schedule or args.all:
        schedules = charging_schedule(vehicle_id, args.verbose)
        for s in schedules:
            print(f"Start Time: {s['startTime']}")
            print(f"Duration: {s['duration']}")
            if not args.privacy:
                print(f"Location: {s['location']['latitude']},{s['location']['longitude']}")
            print(f"Amperage: {s['amperage']}")
            print(f"Enabled: {s['enabled']}")
            print(f"Weekdays: {s['weekDays']}")


    if args.charge_sessions or args.last_charge or args.all:
        sessions = charging_sessions(args.verbose)
        if args.last_charge and sessions:
            sessions = [sessions[-1]]
        for s in sessions:
            if s['energy'] == 0:
                continue
            print(f"Transaction Id: {s['transaction_id']}")
            print(f"Charge Start: {show_local_time(s['charge_start'])}")
            print(f"Charge End: {show_local_time(s['charge_end'])}")
            print(f"Energy added: {s['energy']} kWh")
            eph = s['energy'] / \
                  ((get_local_time(s['charge_end']) - get_local_time(s['charge_start'])).total_seconds() / 3600)
            print(f"Charge rate: {eph:.1f} kW/h")
            print(f"Vendor: {s['vendor']}") if s['vendor'] else None
            if s['range_added']:
                print(f"Range added: {kilometers_to_distance_units(s['range_added'], args.metric):.1f} {distance_units}")
                rph = kilometers_to_distance_units(s['range_added'], args.metric) / \
                      ((get_local_time(s['charge_end']) - get_local_time(s['charge_start'])).total_seconds() / 3600)
                print(f"Range added rate: {rph:.1f} {distance_units}/h")
            print()

    if args.charge_session or args.all:
        session = charging_session(args.verbose)
        if not session:
            print("No active charging session")
        else:
            print(f"Charger ID: {session['chargerId']}")
            print(f"Transaction ID: {session['transactionId']}")
            print(f"Rivian Charger: {session['isRivianCharger']}")
            print(f"Charging Active: {session['vehicleChargerState']['value'] == 'charging_active'}")
            print(f"Charging Updated: {show_local_time(session['vehicleChargerState']['updatedAt'])}")

    if args.live_charging_session or args.all:
        state = get_vehicle_state(vehicle_id, args.verbose)
        s = live_charging_session(vehicle_id=vehicle_id,
                                  verbose=args.verbose)
        if not state or not s:
            print("No live charging session")
        else:
            print(f"Battery Level: {state['batteryLevel']['value']:.1f}%")
            print(f"Range: {kilometers_to_distance_units(state['distanceToEmpty']['value'], args.metric):.1f} {distance_units}")
            print(f"Battery Limit: {state['batteryLimit']['value']:.1f}%")
            print(f"Charging state: {state['chargerState']['value']}")
            print(f"Charger status: {state['chargerStatus']['value']}")

            print(f"Charging Active: {s['vehicleChargerState']['value'] == 'charging_active'}")
            print(f"Charging Updated: {show_local_time(s['vehicleChargerState']['updatedAt'])}")
            print(f"Charge Start: {show_local_time(s['startTime'])}")
            if s['timeElapsed']:
                elapsed_seconds = int(s['timeElapsed'])
                elapsed = get_elapsed_time_string(elapsed_seconds)
                print(f"Elapsed Time: {elapsed}")
            if s['timeRemaining'] and s['timeRemaining']['value']:
                remaining_seconds = int(s['timeRemaining']['value'])
                remaining = get_elapsed_time_string(remaining_seconds)
                print(f"Remaining Time: {remaining}")
            print(f"Charge power: {s['power']['value']} kW")
            print(f"Charge rate: {meters_to_distance_units(s['kilometersChargedPerHour']['value']*1000, args.metric):.1f} {distance_units_string}")
            print(f"Range added: {meters_to_distance_units(s['rangeAddedThisSession']['value']*1000, args.metric):.1f} {distance_units}")
            print(f"Total charged energy: {s['totalChargedEnergy']['value']} kW")
            print(f"State of Charge: {s['soc']['value']:.1f}%")
            print(f"currentMiles: {kilometers_to_distance_units(s['currentMiles']['value'], args.metric):.1f} {distance_units}")
            print(f"current: {s['current']['value']}")

    if args.live_charging_history or args.all:
        s = live_charging_history(vehicle_id=vehicle_id,
                                  verbose=args.verbose)
        start_time = None
        end_time = None
        for d in s:
            print(f"{show_local_time(d['time'])}: {d['kw']} kW")
            if not start_time:
                start_time = get_local_time(d['time'])
            end_time = get_local_time(d['time'])
        if start_time and end_time:
            elapsed = get_elapsed_time_string((end_time - start_time).total_seconds())
            print(f"Elapsed Time: {elapsed}")

    # Work in progress - TODO
    if args.command:
        vehicle_command(args.command, args.vehicle_id, args.verbose)

    if args.all:
        sys.stdout = original_stdout
        print("All commands ran and no exceptions encountered")


if __name__ == '__main__':
    main()

