from ctapipe.image.cleaning import tailcuts_clean
from ctapipe.image.hillas import hillas_parameters
from ctapipe.reco.FitGammaHillas import FitGammaHillas
import numpy as np
from queue import Queue
from astropy import units as u
import time
import os
import gzip
import pickle
import os.path
import logging
from itertools import cycle
from ctapipe.io.containers import InstrumentContainer


def batch_reco(dicts, instrument_dict):
    return [reco(e, instrument_dict) for e in dicts]


def reco(hillas_dict, instrument_dict):
    instrument = InstrumentContainer()
    instrument.__dict__ = instrument_dict

    logging.info('starting reco for dict: {}'.format(hillas_dict))
    tel_phi = {tel_id: 0*u.deg for tel_id in hillas_dict.keys()}
    tel_theta = {tel_id: 20*u.deg for tel_id in hillas_dict.keys()}
    fit_result = FitGammaHillas().predict(
                            hillas_dict,
                            instrument,
                            tel_phi,
                            tel_theta)

    logging.info('Finished reco')
    return fit_result


def batch_hillas(events, geoms):
    return [hillas(e, geoms) for e in events]


def hillas(event, geoms):
    hillas_dict = {}
    for tel_id in event['data']:
        pmt_signal = np.array(event['data'][tel_id]['adc_sums'])
        cam_geom = geoms[tel_id]
        mask = tailcuts_clean(cam_geom, pmt_signal, 1,
                              picture_thresh=10., boundary_thresh=5.)
        pmt_signal[mask == 0] = 0

        moments = hillas_parameters(cam_geom.pix_x,
                                    cam_geom.pix_y,
                                    pmt_signal)

        hillas_dict[tel_id] = moments

    return hillas_dict


def load_event_generator(working_dir):
    p = os.path.join(working_dir, 'resources', 'gammas.pickle.gz')
    with gzip.open(p, 'rb') as f:
        event_list = pickle.load(f)

    events = []
    for e in event_list:
        d = {
             'event_id': np.asscalar(e.dl0.event_id),
             'run_id': e.dl0.run_id,
             'data': {},
             }

        for tel_id, t in e.dl0.tel.items():
            d['data'][tel_id] = {'adc_sums': t.adc_sums[0].tolist()}

        events.append(d)
    return cycle(events)


def load_data(q, events, batch_size=5):
    while True:
        q.put([next(events) for k in range(batch_size)])
        time.sleep(0.12)


def monitor_q(q, name='result queue'):
    while True:
        print('Items currently in {} : {}'.format(name, q.qsize()))
        time.sleep(1)


def get_results(q):
    s = time.time()
    while True:
        results = [q.get() for i in range(q.qsize())]

        if results:
            r = len(results)
            l = len(results[0])
            dt = time.time() - s
            s = time.time()
            print('Latest results: {} elements of length {} in {:.1f} seconds \n'
                  'thats {:.2f} elements per second'.format(r, l, dt,  r*l/dt))

        else:
            print('no results')
        time.sleep(5)


def main():

    def load_instrument(DIR):
        p = os.path.join(DIR, 'resources', 'instrument.pickle.gz')
        with gzip.open(p, 'rb') as f:
            return pickle.load(f)

    def load_cam_geoms(DIR):
        p = os.path.join(DIR, 'resources', 'geoms.pickle.gz')
        with gzip.open(p, 'rb') as f:
            return pickle.load(f)

    from distributed import Client
    client = Client('127.0.0.1:8786')

    generator = load_event_generator('./')
    instrument = load_instrument('./').as_dict()
    geoms = load_cam_geoms('./')

    logging.info('loaded instrument and geometries, distributing to workers')
    instrument_remote = client.scatter(instrument, broadcast=True)
    geometries_remote = client.scatter(geoms, broadcast=True)

    # hillas = partial(hillas, geoms=geometries_remote)
    # reco = partial(reco, instrument=instrument_remote)

    input_q = Queue(maxsize=200)
    remote_queue = client.scatter(input_q)

    hillas_q = client.map(batch_hillas, remote_queue, **{'geoms': geometries_remote})
    reco_q = client.map(batch_reco,  hillas_q, **{'instrument_dict': instrument_remote})

    result_q = client.gather(reco_q)

    from threading import Thread
    Thread(target=load_data, args=(input_q, generator, 40), daemon=True).start()


    #Thread(target=load_data, args=(input_q, generator, 90), daemon=True).start()

    #Thread(target=load_data, args=(input_q, generator, 60), daemon=True).start()
    Thread(target=monitor_q, args=(input_q, 'input queue'), daemon=True).start()

    get_results(result_q)

if __name__ == '__main__':
    main()
