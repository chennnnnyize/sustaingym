"""
GMM training script.

Example command line usage
python -m sustaingym.envs.evcharging.train_gmm_model --site jpl --gmm_n_components 30 --date_range 2019-05-01 2019-08-31 2019-09-01 2019-12-31 2020-02-01 2020-05-31 2021-05-01 2021-08-31

usage: train_gmm_model.py [-h] [--site SITE] [--gmm_n_components GMM_N_COMPONENTS]
                          [--date_ranges DATE_RANGES [DATE_RANGES ...]]

optional arguments:
  -h, --help            show this help message and exit
  --site SITE           Name of site: 'caltech' or 'jpl'
  --gmm_n_components GMM_N_COMPONENTS
  --date_ranges DATE_RANGES [DATE_RANGES ...]
                        Date ranges for GMM models to be trained on.
                        Number of dates must be divisible by 2,
                        with the second later than the first.
                        Dates should be formatted as YYYY-MM-DD.
                        Supported ranges in between 2018-11-01 and 2021-08-31.
"""
from __future__ import annotations

import argparse
from argparse import RawTextHelpFormatter
from collections.abc import Sequence
from datetime import datetime
import os

import numpy as np
import pandas as pd
from sklearn.mixture import GaussianMixture

from .utils import (AM_LA, DEFAULT_SAVE_DIR, DEFAULT_DATE_RANGES, DATE_FORMAT,
                    MINS_IN_DAY, REQ_ENERGY_SCALE, START_DATE, END_DATE,
                    get_real_events, get_folder_name, save_gmm_model,
                    site_str_to_site, SiteStr)


def preprocess(df: pd.DataFrame, filter: bool = True) -> pd.DataFrame:
    """Preprocessing script for real event sessions before GMM modeling.

    Filters EVs with departures / estimated departures on a different date
    than arrival date. The arrival, departure, and estimated departure are
    normalized between 0 and 1 for the time during the day, and the
    requested energy is normalized by a scaling factor.

    Args:
        df: DataFrame of charging events, expected to be gotten from
            get_real_events in the .utils module.
        filter: option to filter cars staying overnight

    Returns:
        Filtered copy of DataFrame with normalized parameters.
    """
    if filter:
        # Filter cars staying overnight
        max_depart = np.maximum(df['departure'], df['estimated_departure'])
        mask = (df['arrival'].dt.day == max_depart.dt.day)
        df = df[mask].copy()

    # Get arrival time, departure time, estimated departure time from datetimes and normalize between [0, 1]
    for col in ['arrival', 'departure', 'estimated_departure']:
        df[col + '_time'] = (df[col].dt.hour * 60 + df[col].dt.minute) / MINS_IN_DAY

    # Normalize requested energy
    df['requested_energy (kWh)'] /= REQ_ENERGY_SCALE

    return df[['arrival_time', 'departure_time', 'estimated_departure_time', 'requested_energy (kWh)']].copy()


def station_id_cnts(df: pd.DataFrame, n2i: dict[str, int]) -> np.ndarray:
    """Returns the usage counts for a network's charging station ids.

    Args:
        df: DataFrame of session observations.
        n2i: dict mapping charging station id to position in numpy array.

    Returns:
        cnts: number of sessions associated with each station id.
    """
    vc = df['station_id'].value_counts()
    cnts = [0 for _ in range(len(n2i))]
    for station_id in vc.index:
        if station_id not in n2i:
            continue
        cnts[n2i[station_id]] = vc[station_id]
    if sum(cnts) == 0:
        raise ValueError('No station ids in DataFrame found in site. ')
    cnts = np.array(cnts, dtype=np.int32)
    return cnts


def parse_string_date_list(date_range: Sequence[str]) -> Sequence[tuple[datetime, datetime]]:
    """Converts a sequence of string date ranges to datetimes.

    Args:
        date_range: an even-length sequence of string dates in the format
            'YYYY-MM-DD'. Each consecutive pair describes a date range, and
            should fall inside the range 2018-11-01 and 2021-08-31.

    Returns:
        A sequence of 2-tuples containing a begin and end datetime.

    Raises:
        ValueError: length of date_range is odd
        ValueError: begin date of pair is not before end date of pair
        ValueError: begin and end date not in data's range
    """
    if len(date_range) % 2 != 0:
        raise ValueError(f'Number of dates must be divisible by 2, found length {len(date_range)} with the second later than the first.')

    date_range_dt = [datetime.strptime(date_str, DATE_FORMAT).replace(tzinfo=AM_LA) for date_str in date_range]
    date_ranges = []
    for i in range(len(date_range) // 2):
        begin, end = date_range_dt[2 * i], date_range_dt[2 * i + 1]

        if begin < START_DATE:
            raise ValueError(f'beginning of date range {date_range[2 * i]} before data start date {START_DATE.strftime(DATE_FORMAT)}')
        if end > END_DATE:
            raise ValueError(f'end of date range {date_range[2 * i + 1]} after data end date {END_DATE.strftime(DATE_FORMAT)}')
        if begin > end:
            raise ValueError(f'beginning of date range {date_range[2 * i]} later than end {date_range[2 * i + 1]}')

        date_ranges.append((begin, end))

    return date_ranges


def create_gmm(site: SiteStr, n_components: int, date_range: tuple[datetime, datetime]) -> None:
    """Creates a custom GMM and saves in the `gmms_ev_charging` folder.

    Args:
        site: either 'caltech' or 'jpl'
        n_components: number of components of Gaussian mixture model
        date_range: an even-length sequence of datetime objects. Each
            consecutive pair describes a date range, and should fall
            inside the range 2018-11-01 and 2021-08-31.
    """
    SAVE_DIR = os.path.join(DEFAULT_SAVE_DIR, site)

    # Get stations
    acn = site_str_to_site(site)
    n2i = {station_id: i for i, station_id in enumerate(acn.station_ids)}

    # check string dates can be converted to datetimes
    date_range_str = tuple(date_range[i].strftime(DATE_FORMAT) for i in range(2))
    range_str = date_range_str[0] + ' ' + date_range_str[1]
    print(f'Getting data from site {site.capitalize()} for date range {range_str}... ')

    # Retrieve events and filter only claimed sessions
    df = get_real_events(date_range[0], date_range[1], site=site)
    df = df[df['claimed']]
    if len(df) == 0:
        print('Empty dataframe, abort GMM training. ')
        return

    num_days_total = (date_range[1] - date_range[0]).days + 1

    # get counts and station ids data
    cnt = df.arrival.dt.date.value_counts().to_numpy()
    num_unseen_days = num_days_total - len(cnt)  # account for days when there are no EVs
    cnt = np.concatenate((cnt, np.zeros(num_unseen_days)))

    sid = station_id_cnts(df, n2i)

    # Preprocess DataFrame for GMM training
    df = preprocess(df)

    print(f'Fitting GMM ({n_components} components, {len(df.columns)} dimensions)... ')
    gmm = GaussianMixture(n_components=n_components)
    gmm.fit(df)

    # Save
    folder_name = get_folder_name(date_range_str[0], date_range_str[1], n_components)
    save_dir = os.path.join(SAVE_DIR, folder_name)
    if not os.path.exists(save_dir):
        print('Creating directory: ', save_dir)
        os.makedirs(save_dir)

    print(f'Saving in: {save_dir}\n')
    save_gmm_model(gmm, cnt, sid, save_dir)


def create_gmms(site: SiteStr, n_components: int, date_ranges: Sequence[tuple[str, str]] = DEFAULT_DATE_RANGES) -> None:
    """
    Creates multiple gmms and saves them in gmm_folder.

    Args:
        site: either 'caltech' or 'jpl'
        n_components: number of components of Gaussian mixture model
        date_range: a sequence of 2-tuples of string dates in the format
            'YYYY-MM-DD'. Each tuple describes a date range, and
            should fall inside the range 2018-11-01 and 2021-08-31.
    """
    print('\n--- Training GMMs ---\n')
    for date_range in date_ranges:
        date_range_dt = parse_string_date_list(date_range)[0]
        create_gmm(site, n_components, date_range=date_range_dt)
    print('--- Training complete. ---\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="GMM Training Script", formatter_class=RawTextHelpFormatter)
    parser.add_argument("--site", default="caltech", help="Name of site: 'caltech' or 'jpl'")
    parser.add_argument("--gmm_n_components", type=int, default=50)
    date_range_help = ("Date ranges for GMM models to be trained on.\n"
                       "Number of dates must be divisible by 2, \nwith the second later than the first. "
                       "\nDates should be formatted as YYYY-MM-DD. "
                       f"\nSupported ranges in between {START_DATE.strftime(DATE_FORMAT)} and {END_DATE.strftime(DATE_FORMAT)}.")
    parser.add_argument("--date_ranges", nargs="+", help=date_range_help)

    args = parser.parse_args()
    if args.date_ranges is None:
        create_gmms(args.site, args.gmm_n_components)
    else:
        date_ranges = [(args.date_ranges[2*i], args.date_ranges[2*i+1]) for i in range(len(args.date_ranges) // 2)]
        create_gmms(args.site, args.gmm_n_components, date_ranges)