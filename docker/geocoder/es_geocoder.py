# Query from database to get address data

from __future__ import print_function, unicode_literals
import psycopg2
import requests
import os
from datetime import datetime, timedelta
from shapely.geometry import Point, LineString
import time


GEOCODE_STATUS_CODES = {
    1: 'Pending',
    2: 'Failed Mapzen',
    3: 'Completed Mapzen',
    4: 'Failed TAMU',
    5: 'Completed TAMU'
}

SQL_COLUMNS = [
    'HOUSEHOLD_ID',
    'ADDRESS_NUMBER',
    'STREET_NAME',
    'STREET_NAME_POST_TYPE',
    'PLACE_NAME',
    'STATE_NAME',
    'ZIP_CODE'
]

# connection_database_name = 'DevVoter'
ES_URL = 'http://elasticsearch/{q_idx}/_search'


def create_query(data, q_type='census'):
    if q_type == 'address':
        query = create_point_query(data)
    elif q_type == 'census':
        query = create_census_query(data)
    else:
        # TODO: Throw specific error
        pass

    for s in data['STREET_NAME'].split(' '):
        query['query']['bool']['must'].append(
            {'term': {"properties.street": s.lower()}}
        )
    return query


def create_point_query(data):
    return {
        'query': {
            'bool': {
                'must': [
                    {'term': {'properties.number': data['ADDRESS_NUMBER']}},
                    {'term': {'properties.state': data['STATE_NAME'].lower()}}
                ],
                'should': [
                    {'term': {'properties.zip': str(data['ZIP_CODE'])}},
                    {'term': {'properties.city': data['PLACE_NAME'].lower()}},
                    {'term': {'properties.street': data['STREET_NAME_POST_TYPE'].lower()}}
                ]
            }
        }
    }


def create_census_query(data):
    return {
        'query': {
            'bool': {
                'must': [
                    {'term': {'properties.state': data['STATE_NAME'].lower()}}
                ],
                'should': [
                    {'term': {'properties.ZIPL': str(data['ZIP_CODE'])}},
                    {'term': {'properties.ZIPR': str(data['ZIP_CODE'])}},
                    {'term': {'properties.FULLNAME': data['STREET_NAME_POST_TYPE'].lower()}}
                ],
                'filter': {
                    'bool': {
                        'should': [
                            {
                                'bool': {
                                    'must': [
                                        {
                                            'bool': {
                                                'should': [
                                                    {'range': {'properties.LFROMHN': {'lte': data['ADDRESS_NUMBER']}}},
                                                    {'range': {'properties.RFROMHN': {'lte': data['ADDRESS_NUMBER']}}}
                                                ]
                                            }
                                        },
                                        {
                                            'bool': {
                                                'should': [
                                                    {'range': {'properties.LTOHN': {'gte': data['ADDRESS_NUMBER']}}},
                                                    {'range': {'properties.RTOHN': {'gte': data['ADDRESS_NUMBER']}}}
                                                ]
                                            }
                                        }
                                    ]
                                }
                            },
                            {
                                'bool': {
                                    'must': [
                                        {
                                            'bool': {
                                                'should': [
                                                    {'range': {'properties.LFROMHN': {'gte': data['ADDRESS_NUMBER']}}},
                                                    {'range': {'properties.RFROMHN': {'gte': data['ADDRESS_NUMBER']}}}
                                                ]
                                            }
                                        },
                                        {
                                            'bool': {
                                                'should': [
                                                    {'range': {'properties.LTOHN': {'lte': data['ADDRESS_NUMBER']}}},
                                                    {'range': {'properties.RTOHN': {'lte': data['ADDRESS_NUMBER']}}}
                                                ]
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        }
    }


def handle_census_range(range_from, range_to):
    if range_from:
        from_int = int(range_from) if range_from.isdigit() else 0
    if range_to:
        to_int = int(range_to) if range_to.isdigit() else 0

    range_even = from_int % 2 == 0 and to_int % 2 == 0

    if from_int > to_int:
        range_diff = from_int - to_int
    else:
        range_diff = to_int - from_int

    return {
        'is_even': range_even,
        'from_int': from_int,
        'to_int': to_int,
        'range_diff': range_diff
    }


def interpolate_census(data, res_data):
    tiger_feat = res_data['_source']
    data_line = LineString([Point(*p) for p in tiger_feat['geometry']])
    line_dist = data_line.distance

    addr_int = int(data['ADDRESS_NUMBER'])
    addr_is_even = addr_int % 2 == 0

    l_range = handle_census_range(tiger_feat['properties']['LFROMHN'],
                                  tiger_feat['properties']['LTOHN'])
    r_range = handle_census_range(tiger_feat['properties']['RFROMHN'],
                                  tiger_feat['properties']['RTOHN'])

    if addr_is_even == l_range['is_even']:
        tiger_range = l_range
    elif addr_is_even == r_range['is_even']
        tiger_range = r_range
    else:
        # Throw error
        pass

    if tiger_range['from_int'] > tiger_range['to_int']:
        range_dist = ((tiger_range['from_int'] - addr_int) / tiger_range['range_diff']) * line_dist
    else:
        range_dist = ((addr_int - tiger_range['from_int']) / tiger_range['range_diff']) * line_dist

    inter_pt = data_line.interpolate(range_dist)
    return {'lat': inter_pt.y, 'lon': inter_pt.x}


# fetch address records to query.  Limit = number of rows requested
def get_unmatched_address_records(cur, limit=1):
    query_address = '''
        SELECT {columns}
        FROM HOUSEHOLD_DIM
        WHERE GEOCODE_STATUS = 1
        LIMIT {limit}
        '''.format(columns=', '.join(SQL_COLUMNS), limit=limit)

    # Run the address query
    cur.execute(query_address)
    return cur.fetchall()


# Request Mapzen, check status
def request_elasticsearch(addr_row, q_type='census'):
    query_data = create_query(addr_row, q_type=q_type)

    response = requests.post(es_url.format(q_idx=q_type), json=query_data)
    # Check headers, status for success and rate limiting
    if response.status_code == 200:
        response_json = response.json()
        if len(response_json) == 0:
            return household_id, None

        if q_type == 'address':
            geom_dict = dict(lon=response_json[0]['geometry']['coordinates'][0],
                             lat=response_json[1]['geometry']['coordinates'][1])
        elif q_type == 'census':
            geom_dict = interpolate_census(addr_row, response_json[0])

        return household_id, geom_dict

    elif response.status_code == 429:
        time.sleep(0.3)
    else:
        return household_id, None


# Either update record to coordinates or change status to failed
def update_address_record(cur, household_id, addr_dict):
    if addr_dict:
        status = 3
        update_statement = '''
            UPDATE household_dim
            SET
                GEOM = ST_SetSRID(ST_MakePoint({lon}, {lat}), 4326),
                GEOCODE_STATUS = {g_status}
            WHERE HOUSEHOLD_ID = {h_id}
            '''.format(lon=addr_dict['lon'],
                       lat=addr_dict['lat'],
                       g_status=status,
                       h_id=household_id)
    else:
        status = 2
        update_statement = '''
            UPDATE household_dim
            SET GEOCODE_STATUS = {g_status}
            WHERE HOUSEHOLD_ID = {h_id}
            '''.format(g_status=status, h_id=household_id)

    cur.execute(update_statement)


def run_geocoder(config, log):
    # Check env var set to postpone execution if hitting rate limits
    wait_time = os.environ.get('WAIT_TIME', None)
    if wait_time:
        wait_dt = datetime.strptime(wait_time, '%Y-%m-%d %H:%M')
        if wait_dt > datetime.now():
            return

    try:
        conn = psycopg2.connect(**config['databases']['DevVoter'])
        cur = conn.cursor()
    except Exception as ex:
        log.error('Connection error with {}: {}'.format(config['databases']['DevVoter']['database'], ex))

    results = get_unmatched_address_records(cur, limit=5)
    result_dicts = [dict(zip(SQL_COLUMNS, r)) for r in results]

    for row in results:
        h_id, geom = request_elasticsearch(row, q_type='census')
        if h_id:
            update_address_record(cur, h_id, geom)
            log.info('Updated address with household id: {}'.format(h_id))
        else:
            time_wait = (datetime.now() + timedelta(hours=1))
            os.environ['WAIT_TIME'] = time_wait.strftime('%Y-%m-%d %H:%M')
            log.info('Setting wait time to {}'.format(os.environ['WAIT_TIME']))
            break
        time.sleep(0.2)

    conn.commit()
    cur.close()
    conn.close()
