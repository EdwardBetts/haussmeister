"""
Module for feeding ThorLabs imaging datasets into a 2p imaging analysis
pipeline

(c) 2015 C. Schmidt-Hieber
GPLv3
"""
from __future__ import print_function
from __future__ import absolute_import

import os
import sys
import shutil
import time
import pickle
import multiprocessing as mp

import numpy as np
import scipy.signal as signal
import scipy.stats as stats
from scipy.io import savemat

import bottleneck

import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

import cv2

import sima
import sima.motion
import sima.segment
import sima.spikes
from sima.ROI import ROIList

try:
    from . import haussio
    from . import movies
    from . import scalebars
    from . import spectral
except ValueError:
    import haussio
    import movies
    import scalebars
    import spectral

import stfio
from stfio import plot as stfio_plot

sys.path.append("%s/py2p/tools" % (
    os.environ["HOME"]))

bar1_color = 'k'   # black
bar2_color = 'w'   # white
edge_color = 'k'   # black
gap2 = 0.15         # gap between series
bar_width = 0.5

NCPUS = int(mp.cpu_count()/2)


class ThorExperiment(object):
    """
    Helper class to feed ThorLabs imaging datasets into a 2p imaging
    analysis pipeline

    Attributes
    ----------
    fn2p : str
        File path (relative to root_path) leading to directory that contains
        tiff series
    ch2p : str
        Channel. Default: "A"
    area2p : str
        Brain area code (e.g. "CA1"). Default: None
    fnsync : str
        Thorsync directory name. Default: None
    fnvr : str
        VR file name trunk. Default: None
    roi_subset : str
        String appended to roi label to distinguish between roi subsets.
        Default: ""
    mc_method : str
        Motion correction method. One of "hmmc", "dft", "hmmcres", "hmmcframe",
        "hmmcpx". Default: "hmmc"
    detrend : bool
        Whether to detrend fluorescence traces. Default: False
    roi_translate : 2-tuple of ints
        Apply ROI translation in x and y. Default: None
    root_path : str
        Root directory leading to fn2p. Default: ""
    """
    def __init__(self, fn2p, ch2p="A", area2p=None, fnsync=None, fnvr=None,
                 roi_subset="", mc_method="hmmc", detrend=False,
                 roi_translate=None, root_path=""):
        self.fn2p = fn2p
        self.ch2p = ch2p
        self.area2p = area2p
        self.fnsync = fnsync
        self.fnvr = fnvr
        self.roi_subset = roi_subset
        self.roi_translate = roi_translate
        self.root_path = root_path
        self.data_path = self.root_path + self.fn2p

        if self.fnsync is not None:
            self.sync_path = os.path.dirname(self.data_path) + "/" + \
                self.fnsync
        else:
            self.sync_path = None

        if self.fnvr is not None:
            self.vr_path = os.path.dirname(self.data_path) + "/" + \
                self.fnvr
        else:
            self.vr_path = None

        self.mc_method = mc_method
        self.mc_suffix = "_mc_" + self.mc_method
        if self.mc_method == "hmmc":
            self.mc_suffix = "_mc"  # special case
            self.mc_approach = sima.motion.HiddenMarkov2D(
                granularity='row', max_displacement=[20, 30],
                verbose=True)
        elif self.mc_method == "dft":
            self.mc_approach = sima.motion.DiscreteFourier2D(
                max_displacement=[20, 30], n_processes=4, verbose=True)
        elif self.mc_method == "hmmcres":
            self.mc_approach = sima.motion.ResonantCorrection(
                sima.motion.HiddenMarkov2D(
                    granularity='row', max_displacement=[20, 30],
                    n_processes=4, verbose=True))
        elif self.mc_method == "hmmcframe":
            self.mc_approach = sima.motion.HiddenMarkov2D(
                granularity='frame', max_displacement=[20, 30],
                n_processes=4, verbose=True)
        elif self.mc_method == "hmmcpx":
            self.mc_approach = sima.motion.HiddenMarkov2D(
                granularity='column', max_displacement=[20, 30],
                n_processes=4, verbose=True)

        self.sima_mc_dir = self.data_path + self.mc_suffix + ".sima"

        self.mc_tiff_dir = self.data_path + self.mc_suffix

        self.movie_mc_fn = self.data_path + self.mc_suffix + ".mp4"

        # Do not add translation string to original roi file name
        self.roi_path_mc = self.data_path + self.mc_suffix + '/RoiSet' + \
            self.roi_subset + '.zip'

        if self.roi_translate is not None:
            self.roi_subset += "_{0}_{1}".format(
                self.roi_translate[0], self.roi_translate[1])

        self.spikefn = self.data_path + self.mc_suffix + "_infer" + \
            self.roi_subset
        self.detrend = detrend
        if self.detrend:
            self.spikefn += "_detrend.pkl"
        else:
            self.spikefn += ".pkl"

        self.proj_fn = self.data_path + self.mc_suffix + "_proj.npy"

    def to_haussio(self, mc=False):
        """
        Convert experiment to haussio.HaussIO

        Parameters
        ----------
        mc : bool, optional
            Use motion corrected images. Default: False

        Returns
        -------
        dataset : haussio.ThorHaussIO
            A haussio.ThorHaussio instance
        """
        if not mc:
            return haussio.ThorHaussIO(self.data_path, chan=self.ch2p,
                                       sync_path=self.sync_path)
        else:
            return haussio.ThorHaussIO(
                self.data_path + self.mc_suffix,
                chan=self.ch2p, xml_path=self.data_path+"/Experiment.xml",
                sync_path=self.sync_path)

    def to_sima(self, mc=False):
        """
        Convert experiment to sima.ImagingDataset

        Parameters
        ----------
        mc : bool, optional
            Use motion corrected images. Default: False

        Returns
        -------
        dataset : sima.ImagingDataset
            A sima.ImagingDataset instance
        """
        if mc:
            suffix = self.mc_suffix
        else:
            suffix = ""
        sima_dir = self.data_path + suffix + ".sima"
        if not os.path.exists(sima_dir):
            raise IOError(
                "Couldn't find sima file " + sima_dir)

        dataset = sima.ImagingDataset.load(sima_dir)
        try:
            dataset = sima.ImagingDataset.load(sima_dir)
            restore = False
        except EOFError:
            restore = True
        except IndexError:
            restore = True

        if not restore:
            try:
                dataset.channel_names.index(self.ch2p)
                dataset.sequences
            except (ValueError, IOError):
                restore = True

        if restore:
            experiment = self.to_haussio(mc=mc)
            shutil.rmtree(experiment.sima_dir)
            dataset = experiment.tosima(stopIdx=None)

        return dataset


def thor_batch(data):
    """
    Read in ThorImage dataset, apply motion correction, export motion-corrected
    tiffs, produce movie of corrected and uncorrected data

    Parameters
    ----------
    data : ThorExperiment
        The ThorExperiment to be processed
    """
    experiment = data.to_haussio(mc=False)

    if os.path.exists(experiment.movie_fn):
        raw_movie = movies.html_movie(experiment.movie_fn)
    else:
        try:
            raw_movie = experiment.make_movie(norm=14.0, crf=28)
        except IOError:
            raw_movie = experiment.make_movie(norm=False, crf=28)

    if not os.path.exists(experiment.sima_dir):
        dataset = experiment.tosima(stopIdx=None)
    else:
        dataset = data.to_sima(mc=False)

    if not os.path.exists(data.sima_mc_dir):
        dataset_mc = data.mc_approach.correct(dataset, data.sima_mc_dir)
    else:
        dataset_mc = data.to_sima(mc=True)

    if not os.path.exists(
            data.mc_tiff_dir + "/" + os.path.basename(experiment.filenames[-1])):
        print("Exporting frames...")
        haussio.sima_export_frames(dataset_mc, data.mc_tiff_dir,
                                   experiment.filenames)

    if os.path.exists(data.movie_mc_fn):
        corr_movie = movies.html_movie(data.movie_mc_fn)
    else:
        try:
            corr_movie = experiment.make_movie_extern(
                data.mc_tiff_dir, norm=14.0, crf=28)
        except IOError:
            corr_movie = experiment.make_movie_extern(
                data.mc_tiff_dir, norm=False, crf=28)


def activity_level(data, infer_threshold=0.15, roi_subset=""):
    """
    Determine the ratio of active over inactive neurons

    Parameters
    ----------
    data : ThorExperiment
        The ThorExperiment to be processed
    infer_threshold : float, optional
        Activity threshold of spike inference. Default: 0.15
    roi_subset : str
        Roi subset to be processed

    Returns
    -------
    level : int, int
        Number of active and inactive neurons
    """

    if not os.path.exists(data.spikefn):
        print("Couldn't spike inference file", data.spikefn)
        return None, None

    spikefile = open(data.spikefn, 'rb')
    spikes = pickle.load(spikefile)
    fits = pickle.load(spikefile)
    parameters = pickle.load(spikefile)
    spikefile.close()

    active = 0
    for nroi in range(spikes[0].shape[0]):
        sys.stdout.write("\rROI %d/%d" % (nroi+1, spikes[0].shape[0]))
        sys.stdout.flush()

        spikes_filt = spikes[nroi][1:]
        event_ts = stfio.peak_detection(spikes_filt, infer_threshold, 30)
        active += (len(event_ts) > 0)

    sys.stdout.write("\n%s: %d out of %d cells (%.0f%%) are active\n" % (
        data.data_path, active, spikes[0].shape[0],
        100.0*active/spikes[0].shape[0]))

    return active, spikes[0].shape[0]


def process_data(data, dt, detrend=False, base_fraction=0.05):
    """
    Compute \Delta F / F_0 and detrend if required

    Parameters
    ----------
    data : numpy.ndarray
        Fluorescence trace
    infer_threshold : dt
        Frame interval
    detrend : bool, optional
        Detrend fluorescence traces. Default: False
    base_fraction : float, optional
        Bottom fraction to be used for F_0 computation. If None, F_0 is set to
        data.mean(). Default: 0.05

    Returns
    -------
    ret_data : numpy.ndarray
        Processed data
    """
    # return highpass(
    #     (data-data.mean())/data.mean() * 100.0, dt, highpass_f)
    if base_fraction is None:
        F0 = data.mean()
    else:
        F0 = data[data.argsort()][
            :int(np.round(base_fraction*data.shape[0]))].mean()

    ret_data = (data-F0)/F0 * 100.0

    if detrend:
        ret_data = signal.detrend(ret_data,
                                  bp=[0, int(data.shape[0]/4.0),
                                      int(data.shape[0]/2.0),
                                      int(3.0*data.shape[0]/4.0),
                                      int(data.shape[0])])

    return ret_data


def xcorr(data, chan, roi_subset1="DG", roi_subset2="CA3",
          infer_threshold=0.15):
    """
    Compute cross correlation between fluorescence extracted from two
    roi subsets

    Parameters
    ----------
    data : ThorExperiment
        The ThorExperiment to be processed
    chan : str
        Channel characted
    roi_subset1 : str, optional
        Roi subset 1 suffix. Default: "DG"
    roi_subset2 : str, optional
        Roi subset 2 suffix. Default: "CA3"
    infer_threshold: float, optional
        Spike inference threshold. Default: 0.15

    Returns
    -------
    high_xcs : list of 2-tuple of ints
        List of roi indices with xcorr values > 0.5
    """

    rois1, meas1, experiment1, seq1, spikes1 = \
        get_rois_ij(data, infer=True)

    rois2, meas2, experiment2, seq2, spikes2 = \
        get_rois_ij(data, infer=True)

    meas1_filt = [process_data(m1, experiment1.dt, detrend=data.detrend)
                  for m1 in meas1]
    meas2_filt = [process_data(m2, experiment2.dt, detrend=data.detrend)
                  for m2 in meas2]
    high_xcs = []
    for nroi1, m1 in enumerate(meas1_filt):
        spikes1_filt = (spikes1[0][nroi1]-spikes1[0][nroi1].min())[1:]
        event1_ts = stfio.peak_detection(spikes1_filt, infer_threshold, 30)
        if len(event1_ts):
            for nroi2, m2 in enumerate(meas2_filt):
                spikes2_filt = (spikes2[0][nroi2]-spikes2[0][nroi2].min())[1:]
                event2_ts = stfio.peak_detection(spikes2_filt, infer_threshold, 30)
                if len(event2_ts):
                    xc = cv2.matchTemplate(m1, m2, cv2.TM_CCORR_NORMED)
                    if xc.max() > 0.5:
                        high_xcs.append((nroi1, nroi2))

    return high_xcs


def norm(sig):
    """
    Normalize data to have range [0,1]

    Parameters
    ----------
    sig : numpy.ndarray
        Data to be normalized

    Returns
    -------
    norm : numpy.ndarray
        Normalized data
    """
    return (sig-sig.min())/(sig.max()-sig.min())


def plot_rois(rois, measured, experiment, seq, data_path, pdf_suffix="",
              spikes=None, infer_threshold=0.15, region="", vrdict=None,
              lopass=1.0):

    """
    Plot ROIs on top of z-projected image, extracted fluorescence, spike
    inference, fluorescence and spike inference against position (if available)

    Parameters
    ----------
    rois : sima.ROI.ROIList
        sima ROIList to be plotted
    measured : numpy.ndarray
        Processed fluorescence data for each ROI
    experiment : haussio.HaussIO
        haussio.HaussIO instance
    seq : numpy.ndarray
        z-projected fluorescence image
    data_path : str
        Path to data directory
    pdf_suffix : str, optional
        Suffix appended to pdf. Default: ""
    spikes : numpy.ndarray, optional
        Spike inference values. Default: None
    infer_threshold: float, optional
        Spike inference threshold. Default: 0.15
    region : str, optional
        Brain region. Default: ""
    vrdict : dict, optional
        Dictionary containing processed VR data. Default: None
    lopass : float, optional
        Lowpass filter frequency for plotted traces. Default: 1.0
    """
    fig = plt.figure(figsize=(18, 24))
    colors = ['r', 'g', 'b', 'c', 'm', 'y']

    nrows = 8
    strow = 2
    if vrdict is None:
        stcol = 0
        ncols = 2
    else:
        stcol = 2
        ncols = 4

    gs = gridspec.GridSpec(nrows, ncols)
    ax_nospike = fig.add_subplot(gs[strow:, 1:2])
    plt.axis('off')

    ax2 = fig.add_subplot(gs[:strow, stcol:])
    ax2.imshow(seq, cmap='gray')
    experiment.plot_scale_bar(ax2)
    plt.axis('off')

    ax_spike = fig.add_subplot(gs[strow:, 0:1])
    plt.axis('off')

    pos_spike, pos_nospike = 0, 0
    if spikes is not None:
        if spikes.shape[0] != len(rois):
            raise AssertionError(
                "Number of ROIs is {0}, number of spike inferences "
                "is {1}".format(len(rois), spikes.shape[0]))

        assert(spikes.shape[0] == len(rois))

    ndiscard = 5

    if vrdict is not None:
        dtvr = np.mean(np.diff(vrdict['t_vr']))*1e-3
        ax_pos = stfio_plot.StandardAxis(fig, gs[1, 0:1], hasx=False,
                                         sharex=ax_spike)
        ax_pos_nospike = stfio_plot.StandardAxis(
            fig, gs[1, 1:2], hasx=False, hasy=False,
            sharex=ax_nospike, sharey=ax_pos)
        ax_pos.plot(vrdict['t_vr']*1e-3, vrdict['posy_vr'])
        ax_pos_nospike.plot(vrdict['t_vr']*1e-3, vrdict['posy_vr'])
        ax_pos.set_ylabel("VR position (m)")
        # ax_pos.set_ylim(vrdict['posy_vr'].min(), vrdict['posy_vr'].max())
        for ev in vrdict['events']:
            if ev.evcode in [b'GZ', b'GL', b'GN', b'GH', b'TP', b'UP', b'UR']:
                ax_pos.plot(ev.time, -0.05, ev.marker, mec='none', ms=ev.ms)
        ax_speed = stfio_plot.StandardAxis(
            fig, gs[0, 0:1], hasx=False, sharex=ax_spike)
        ax_speed_nospike = stfio_plot.StandardAxis(
            fig, gs[0, 1:2], hasx=False, hasy=False,
            sharex=ax_nospike, sharey=ax_speed)
        ax_speed.plot(vrdict['t_vr'][:-1]*1e-3+dtvr/2.0, vrdict['speed_vr'])
        ax_speed_nospike.plot(
            vrdict['t_vr'][:-1]*1e-3+dtvr/2.0, vrdict['speed_vr'])
        ax_speed.set_ylabel("Speed (m/s)")
        ax_speed.set_ylim(vrdict['speed_vr'].min(), vrdict['speed_vr'].max())

        ax_maps_fluo = stfio_plot.StandardAxis(
            fig, gs[strow:, 2:3], hasx=True, hasy=False, sharey=ax_spike)
        if spikes is not None:
            ax_maps_infer = stfio_plot.StandardAxis(
                fig, gs[strow:, 3:4], hasx=True, hasy=False, sharey=ax_spike)

    for nroi, roi in enumerate(rois):
        sys.stdout.write("\rROI %d/%d" % (nroi+1, len(rois)))
        sys.stdout.flush()
        if lopass is not None:
            measured_float = measured[nroi, :].astype(np.float)
            meas_filt = spectral.lowpass(
                stfio_plot.Timeseries(measured_float, experiment.dt),
                lopass, verbose=False).data[ndiscard:]
        else:
            meas_filt = measured[nroi, ndiscard:]
        meas_filt -= meas_filt.min()
        if nroi == 0:
            normamp = meas_filt.max() - meas_filt.min()

        try:
            ax2.plot(roi.coords[0][:, 0], roi.coords[0][:, 1],
                     colors[nroi % len(colors)])
            ax2.text(roi.coords[0][0, 0], roi.coords[0][0, 1],
                     "{0}".format(nroi+1),
                     color=colors[nroi % len(colors)],
                     fontsize=10)
        except sima.ROI.NonBooleanMask:
            print("NonBooleanMask")

        if vrdict is None:
            trange = np.arange(len(meas_filt))*experiment.dt
        else:
            trange = vrdict['t_2p'][ndiscard:] * 1e-3

        ax = ax_spike
        pos = pos_spike

        if spikes is not None:
            spikes_filt = spikes[nroi][1:]
            if infer_threshold is not None:
                event_ts = stfio.peak_detection(
                    spikes_filt, infer_threshold, 30)
                if len(event_ts) == 0:
                    ax = ax_nospike
                    pos = pos_nospike
            else:
                spikes_filt -= spikes_filt.min()
                ax_nospike.plot(
                    trange[1:], spikes_filt[
                        ndiscard:ndiscard+len(trange[1:])] /
                    spikes_filt[
                        ndiscard:ndiscard+len(trange[1:])].max() *
                    normamp + pos,
                    colors[nroi % len(colors)])
        fontweight = 'normal'
        fontsize = 14
        ax.plot(
            trange, meas_filt[:len(trange)]-meas_filt[:len(trange)].min()+pos,
            colors[nroi % len(colors)])
        ax.text(0, (meas_filt-meas_filt.min()+pos).mean(),
                "{0}".format(nroi + 1),
                color=colors[nroi % len(colors)], ha='right',
                fontweight=fontweight, fontsize=fontsize)
        if vrdict is not None:
            fluo = norm(vrdict['fluomap'][nroi][1]) * normamp
            fluo -= fluo.min()
            ax_maps_fluo.plot(vrdict['fluomap'][nroi][0],
                              fluo + pos,
                              colors[nroi % len(colors)])
            if spikes is not None:
                infer = norm(vrdict['infermap'][nroi][1]) * normamp
                infer -= infer.min()
                ax_maps_infer.plot(vrdict['infermap'][nroi][0],
                                   infer + pos,
                                   colors[nroi % len(colors)])
        if infer_threshold is None:
            pos_spike += meas_filt.max()-meas_filt.min()+1.0
        elif len(event_ts):
            pos_spike += meas_filt.max()-meas_filt.min()+1.0
        else:
            pos_nospike += meas_filt.max()-meas_filt.min()+1.0

    sys.stdout.write("\n")
    scalebars.add_scalebar(ax_spike)
    if infer_threshold is not None:
        scalebars.add_scalebar(ax_nospike)

    if region is None:
        regionstr = "undefined region"
    else:
        regionstr = region

    fig.suptitle(experiment.filetrunk.replace("_", " ") + " " + regionstr,
                 fontsize=18)
    if vrdict is not None:
        ax_maps_fluo.set_xlim(vrdict['posy_vr'].min(), vrdict['posy_vr'].max())
        ax_maps_fluo.set_xlabel("VR position (m)")
        if spikes is not None:
            ax_maps_infer.set_xlim(vrdict['posy_vr'].min(), vrdict['posy_vr'].max())
            ax_maps_infer.set_xlabel("VR position (m)")

    plt.savefig(data_path + "_rois3" + pdf_suffix + ".pdf")


def infer_spikes(dataset, signal_label):
    """
    Perform spike inference

    Parameters
    ----------
    dataset : sima.Imaging.Dataset
        Dataset to be processed
    signal_label : str
        Label of signal to be processed

    Returns
    -------
    inference : ndarray of float
        The inferred normalized spike count at each time-bin.  Values are
        normalized to the maximium value over all time-bins.
    fit : ndarray of float
        The inferred denoised fluorescence signal at each time-bin.
    parameters : dict
        Dictionary with values for 'sigma', 'gamma', and 'baseline'.

    """
    try:
        res = dataset.infer_spikes(
            label=signal_label, gamma=None, share_gamma=True,
            mode=u'correct', verbose=False)
    except:
        res = dataset.infer_spikes(
            label=signal_label, gamma=None, share_gamma=False,
            mode=u'correct', verbose=False)

    return res


def affine_transform_matrix(dx, dy):
    """
    Compute affine transformations

    Parameters
    ----------
    dx : int
        Shift in x
    dy : int
        Shift in y

    Returns
    -------
    matrix : numpy.ndarray
        2x3 numpy array to be used by roi.transform
    """
    return [np.array([
        [1, 0, dx],
        [0, 1, dy]])]


def extract_signals(signal_label, rois, data, infer=True):
    """
    Extract fluorescence data from ROIs

    Parameters
    ----------
    signal_label : str
        Label of signal to be extracted
    rois : sima.ROI.ROIList
        sima ROIList to be plotted
    data : ThorExperiment
        The ThorExperiment to be processed
    infer : bool, optional
        Perform spike inference. Default: True

    Returns
    -------
    measured : numpy.ndarray
        Processed fluorescence data for each ROI
    seq : numpy.ndarray
        z-projected fluorescence image
    spikes : numpy.ndarray
        Spike inference for each ROI
    """

    experiment = data.to_haussio()
    dataset = data.to_sima(mc=True)

    sys.stdout.write(
        "Extracting signals with label {0}... ".format(signal_label))
    sys.stdout.flush()
    t0 = time.time()
    measured, seq = extract_rois(
        signal_label, dataset, rois, data, experiment.dt)
    sys.stdout.write("done (took %.2fs)\n" % (time.time()-t0))
    sys.stdout.flush()

    measured[np.isnan(measured)] = 0

    assert(np.sum(np.isnan(measured)) == 0)

    if infer:
        sys.stdout.write("Inferring spikes... ")
        sys.stdout.flush()
        t0 = time.time()
        if not os.path.exists(data.spikefn):
            spikes, fits, parameters = infer_spikes(dataset, signal_label)
            spikefile = open(data.spikefn, 'wb')
            pickle.dump(spikes, spikefile)
            pickle.dump(fits, spikefile)
            pickle.dump(parameters, spikefile)
            spikefile.close()
            spikes = spikes[0]
        else:
            spikefile = open(data.spikefn, 'rb')
            spikes = pickle.load(spikefile)[0]
            fits = pickle.load(spikefile)[0]
            parameters = pickle.load(spikefile)[0]
            spikefile.close()

        sys.stdout.write(
            "done (took %.2fs)\n" % (time.time()-t0))
        spikes = np.array([spike-spike[1:].min() for spike in spikes])
    else:
        spikes = measured

    return measured, seq, spikes


def get_rois_ij(data, infer=True):
    """
    Extract fluorescence data from ImageJ ROIs

    Parameters
    ----------
    data : ThorExperiment
        The ThorExperiment to be processed
    infer : bool, optional
        Perform spike inference. Default: True

    Returns
    -------
    rois : sima.ROI.ROIList
        sima ROIList to be plotted
    measured : numpy.ndarray
        Processed fluorescence data for each ROI
    experiment : haussio.HaussIO
        haussio.HaussIO instance
    seq : numpy.ndarray
        z-projected fluorescence image
    spikes : numpy.ndarray
        Spike inference values
    """
    if not os.path.exists(data.roi_path_mc):
        print("Couldn't find ImageJ ROIs in", data.roi_path_mc)
        return

    experiment = data.to_haussio()
    dataset_mc = data.to_sima(mc=True)

    dataset_mc.delete_ROIs('from_ImageJ' + data.roi_subset)
    rois = ROIList.load(data.roi_path_mc, fmt='ImageJ')
    dataset_mc.add_ROIs(rois, 'from_ImageJ' + data.roi_subset)
    if data.roi_translate is not None:
        rois = rois.transform(affine_transform_matrix(
            data.roi_translate[0], data.roi_translate[1]))

    signal_label = 'imagej_rois' + data.roi_subset

    measured, seq, spikes = extract_signals(
        signal_label, rois, data, infer=infer)

    return rois, measured, experiment, seq, spikes


def get_rois_sima(data, infer=True):
    """
    Extract fluorescence data from ROIs that are identified
    by sima's stICA

    Parameters
    ----------
    data : ThorExperiment
        The ThorExperiment to be processed
    infer : bool, optional
        Perform spike inference. Default: True

    Returns
    -------
    rois : sima.ROI.ROIList
        sima ROIList to be plotted
    measured : numpy.ndarray
        Processed fluorescence data for each ROI
    experiment : haussio.HaussIO
        haussio.HaussIO instance
    seq : numpy.ndarray
        z-projected fluorescence image
    spikes : numpy.ndarray
        Spike inference values
    """
    experiment = data.to_haussio()
    dataset_mc = data.to_sima(mc=True)

    if not('from_sima_stICA' in dataset_mc.ROIs.keys()):
        print("Running sima stICA... ")
        t0 = time.time()
        stica_approach = sima.segment.STICA(components=50, verbose=True)
        stica_approach.append(
            sima.segment.SparseROIsFromMasks(min_size=80.0,
                                             n_processes=NCPUS))
        stica_approach.append(
            sima.segment.SmoothROIBoundaries(n_processes=NCPUS))
        stica_approach.append(
            sima.segment.MergeOverlapping(threshold=0.5))

        rois = dataset_mc.segment(stica_approach, 'from_sima_stICA')
        print("sima stICA took {0:.2f}".format(time.time()-t0))
    else:
        rois = dataset_mc.ROIs['from_sima_stICA']

    if data.roi_translate is not None:
        rois = rois.transform(affine_transform_matrix(
            data.roi_translate[0], data.roi_translate[1]))

    signal_label = 'sima_stICA_rois' + data.roi_subset

    measured, seq, spikes = extract_signals(
        signal_label, rois, data, infer=infer)

    return rois, measured, experiment, seq, spikes


def get_rois_thunder(data, tsc, infer=True, stopIdx=None):
    """
    Extract fluorescence data from ROIs that are identified
    by thunder's ICA

    Parameters
    ----------
    data : ThorExperiment
        The ThorExperiment to be processed
    infer : bool, optional
        Perform spike inference. Default: True

    Returns
    -------
    rois : sima.ROI.ROIList
        sima ROIList to be plotted
    measured : numpy.ndarray
        Processed fluorescence data for each ROI
    experiment : haussio.HaussIO
        haussio.HaussIO instance
    seq : numpy.ndarray
        z-projected fluorescence image
    spikes : numpy.ndarray
        Spike inference values
    """
    experiment = data.to_haussio()
    dataset_mc = data.to_sima(mc=True)

    thunder_roiraw_fn = data.data_path + "_thunder_rois.npy"
    if not os.path.exists(thunder_roiraw_fn):
        from thunder import ICA

        print("Reading files into thunder... ")

        data_thunder = tsc.loadImages(data.mc_tiff_dir, inputFormat='tif',
                                      stopIdx=stopIdx)
        data_thunder.cache()
        data_thunder.count()

        data_time_series = data_thunder.toSeries().toTimeSeries()
        data_time_series.cache()
        data_time_series.count()

        print("Running thunder ICA... ")
        t0 = time.time()
        model = ICA(k=100, c=50, svdMethod='em').fit(data_time_series)
        print("Thunder ICA took {0:.2f} s".format(time.time()-t0))

        imgs = model.sigs.pack()

        np.save(thunder_roiraw_fn, imgs)

    else:

        imgs = np.load(thunder_roiraw_fn)

    thunder_roi_fn = data.data_path + "_rois_thunder.pkl"
    if not os.path.exists(thunder_roi_fn):
        rois = ROIList([sima.ROI.ROI(img) for img in imgs])

        sparsify = sima.segment.SparseROIsFromMasks(min_size=80.0,
                                                    n_processes=NCPUS)
        smoothen = sima.segment.SmoothROIBoundaries(n_processes=NCPUS)
        merge = sima.segment.MergeOverlapping(threshold=0.5)
        t0 = time.time()
        print("Postprocessing... ")
        rois = merge.apply(
            smoothen.apply(
                sparsify.apply(
                    rois)))
        print("Postprocessing took {:.2f}".format(time.time()-t0))
        rois.save(thunder_roi_fn)
    else:
        rois = ROIList.load(thunder_roi_fn)

    if data.roi_translate is not None:
        rois = rois.transform(affine_transform_matrix(
            data.roi_translate[0], data.roi_translate[1]))

    dataset_mc.delete_ROIs('from_thunder_ICA')
    dataset_mc.add_ROIs(rois, 'from_thunder_ICA')

    signal_label = 'thunder_ICA_rois' + data.roi_subset

    measured, seq, spikes = extract_signals(
        signal_label, rois, data, infer=infer)

    return rois, measured, experiment, seq, spikes


def get_vr_data(data, measured, spikes):
    """
    Read and assemble VR data

    Parameters
    ----------
    data : ThorExperiment
        The ThorExperiment to be processed
    measured : numpy.ndarray
        Processed fluorescence data for each ROI
    spikes : numpy.ndarray
        Spike inference values

    Returns
    -------
    vrdict : dict
        Dictionary with processed VR data
    """
    import syncfiles

    if data.fnvr is not None:
        vrtimes, framet2p, frametvr, posy, speedvr, fluomap, infermap, evlist, timeev = \
            syncfiles.assemble_files_2p(data, measured, spikes)
        t_ev_matlab = [ev.time for ev in evlist
                       if ev.evcode in [
                           b'GZ', b'GL', b'GN', b'GH', b'TP', b'UP', b'UR']]
        events_matlab = [ev.evcode.decode() for ev in evlist
                         if ev.evcode in [
                             b'GZ', b'GL', b'GN', b'GH', b'TP', b'UP', b'UR']]
        if infermap is None:
            infermap = [0]
        vrdict = {
            "t_2p": framet2p,
            "DFoF_2p": measured,
            "t_vr": frametvr,
            "posy_vr": posy,
            "speed_vr": speedvr,
            "t_ev": timeev,
            "events": evlist,
            "t_ev_matlab": t_ev_matlab,
            "events_matlab": events_matlab,
            'fluomap': fluomap,
            'infermap': infermap}
        savemat(data.data_path + "_vr.mat", vrdict)
        return vrdict
    else:
        return None


def thor_batch_roi_ij(data, infer=True, infer_threshold=0.15):
    """
    Extract and process fluorescence data from ImageJ ROIs

    Parameters
    ----------
    data : ThorExperiment
        The ThorExperiment to be processed
    infer : bool, optional
        Perform spike inference. Default: True
    infer_threshold : float, optional
        Activity threshold of spike inference. Default: 0.15
    """
    rois, measured, experiment, seq, spikes = \
        get_rois_ij(data, infer)

    vrdict = get_vr_data(data, measured, spikes)

    plot_rois(rois, measured, experiment, seq, data.data_path,
              pdf_suffix="_ij", spikes=spikes, region=data.area2p,
              infer_threshold=infer_threshold, vrdict=vrdict)


def thor_batch_roi_sima(data, infer=True, infer_threshold=0.15):
    """
    Extract and process fluorescence data from ROIs that are identified
    by sima's stICA

    Parameters
    ----------
    data : ThorExperiment
        The ThorExperiment to be processed
    infer : bool, optional
        Perform spike inference. Default: True
    infer_threshold : float, optional
        Activity threshold of spike inference. Default: 0.15
    """
    rois, measured, experiment, seq, spikes = \
        get_rois_sima(data, infer)

    vrdict = get_vr_data(data, measured, spikes)

    plot_rois(rois, measured, experiment, seq, data.data_path,
              pdf_suffix="_sima", spikes=spikes, region=data.area2p,
              infer_threshold=infer_threshold, vrdict=vrdict)


def thor_batch_roi_thunder(data, tsc, infer=True, infer_threshold=0.15,
                           stopIdx=None):
    """
    Extract and process fluorescence data from ROIs that are identified
    by thunder's ICA

    Parameters
    ----------
    data : ThorExperiment
        The ThorExperiment to be processed
    infer : bool, optional
        Perform spike inference. Default: True
    infer_threshold : float, optional
        Activity threshold of spike inference. Default: 0.15
    """
    rois, measured, experiment, seq, spikes = \
        get_rois_thunder(data, tsc, infer, stopIdx=stopIdx)

    vrdict = get_vr_data(data, measured, spikes)

    plot_rois(rois, measured, experiment, seq, data.data_path,
              pdf_suffix="_thunder", spikes=spikes, region=data.area2p,
              infer_threshold=infer_threshold, vrdict=vrdict)


def eta(measured, vrdict, evcodelist):
    """
    Compute event-triggered average of fluorescence data

    Parameters
    ----------
    measured : numpy.ndarray
        Processed fluorescence data for each ROI
    vrdict : dict
        Dictionary with processed VR data
    evcodelist : list of str
        List of event types to trigger on
    """
    pre_sd = 1.5
    post_sd = 1.5
    fig = plt.figure()
    for nev, evcode in enumerate(evcodelist):
        nsub = len(evcodelist*100) + 10 + nev + 1
        ax = fig.add_subplot(nsub)
        plt.axis('off')
        tpre = 1.0
        tpost = 4.0
        evtime = -1.0
        nocc = 0
        roilist = []
        for ev in vrdict['events']:
            if ev.evcode == evcode and (ev.time-evtime > 0.25):
                evtime = ev.time
                t0 = (ev.time-tpre) * 1e3
                te = ev.time * 1e3
                tf = (ev.time+1.5) * 1e3
                for nm, meas in enumerate(measured):
                    fluorange_pre = meas[
                        (vrdict['t_2p'] >= t0) &
                        (vrdict['t_2p'] < te)]
                    fluorange_find = meas[
                        (vrdict['t_2p'] >= te) &
                        (vrdict['t_2p'] < tf)]
                    if fluorange_find.max() > meas.mean()+post_sd*meas.std() and \
                       fluorange_pre.max() < meas.mean()+pre_sd*meas.std():
                        if nocc == 0:
                            roilist.append(nm)
                    else:
                        if nm in roilist:
                            roilist.remove(nm)
                nocc += 1

        print(nocc, len(roilist), roilist)
        evtime = -1.0
        for ev in vrdict['events']:
            if ev.evcode == evcode and (ev.time-evtime > 0.25):
                evtime = ev.time
                t0 = (ev.time-tpre) * 1e3
                t1 = (ev.time+tpost) * 1e3
                trange = vrdict['t_2p'][
                    (vrdict['t_2p'] >= t0) &
                    (vrdict['t_2p'] < t1)]
                for nm, meas in enumerate(measured):
                    if nm in roilist:
                        fluorange = meas[
                            (vrdict['t_2p'] >= t0) &
                            (vrdict['t_2p'] < t1)]
                        ax.plot(trange-trange[0], fluorange)

        ax.plot(tpre*1e3, -50.0, 'ok')

    scalebars.add_scalebar(ax)


def thor_gain_roi_ij(exp_list, infer=True, infer_threshold=0.15):
    for data in exp_list:
        rois, measured, experiment, seq, spikes = \
            get_rois_ij(data, infer)

        vrdict = get_vr_data(data, measured, spikes)

        eta(measured, vrdict, [b'TP', b'GZ', b'GH'])


def compare_rois(rois1, rois2):
    if len(rois1) != len(rois2):
        return False

    # for roi1, roi2 in zip(rois1, rois2):
    #     try:
    #         roi1.coords
    #         roi2.coords
    #     except AttributeError:
    #         return False
    #     if roi1.coords != roi2.coords:
    #         return False

    return True


def extract_rois(signal_label, dataset, rois, data, dt):
    """
    Extract fluorescence data from ROIs

    Parameters
    ----------
    signal_label : str
        Label of signal to be extracted
    dataset : sima.Imaging.Dataset
        sima dataset
    rois : sima.ROI.ROIList
        sima ROIList
    data : ThorExperiment
        The ThorExperiment to be processed
    dt : Frame interval

    Returns
    -------
    measured : numpy.ndarray
        Processed fluorescence data for each ROI
    seq : numpy.ndarray
        z-projected fluorescence image
    """
    if signal_label in dataset.signals().keys():
        signals = dataset.signals()[signal_label]
        if not compare_rois(dataset.signals()[signal_label]['rois'],
                            rois):
            signals = dataset.extract(rois, label=signal_label,
                                      save_summary=False, n_processes=NCPUS)
    else:
        signals = dataset.extract(rois, label=signal_label,
                                  save_summary=False, n_processes=NCPUS)

    measured = np.array([process_data(meas, dt, detrend=data.detrend)
                         for meas in signals['raw'][0]])

    if not os.path.exists(data.proj_fn):
        try:
            seq = bottleneck.nanmax(
                np.array(
                    dataset.sequences[0][:, 0, :, :, 0]).squeeze(), axis=0)
        except MemoryError:
            nframes = dataset.sequences[0].shape[0]
            nseqs = 32
            nsubseqs = int(nframes)/nseqs
            seq = bottleneck.nanmax(np.array([
                bottleneck.nanmax(
                    np.array(dataset.sequences[0][
                        nseq*nsubseqs:(nseq+1)*nsubseqs, 0, :, :, 0]
                    ).squeeze(), axis=0)
                for nseq in range(nseqs)
            ]), axis=0)

        np.save(data.proj_fn, seq)
    else:
        seq = np.load(data.proj_fn)

    return measured, seq


class Bardata(object):
    def __init__(self, mean, err=None, data=None, title="", color=bar1_color):
        self.mean = mean
        self.err = err
        self.data = data
        self.title = title
        self.color = color


def make_bardata(data, title='', color='k'):
    return Bardata(np.mean(data), err=stats.sem(data), data=data, title=title,
                   color=color)


def bargraph(datasets, ax, ylabel=None, labelpos=0, ylim=0, paired=False,
             xdata=None, bar=True, ms=15):

    xret = []

    if paired:
        assert(len(datasets) == 2)
        assert(datasets[0].data is not None and datasets[1].data is not None)
        assert(len(datasets[0].data) == len(datasets[1].data))

    ax.axis["right"].set_visible(False)
    ax.axis["top"].set_visible(False)
    if xdata is None:
        ax.axis["bottom"].set_visible(False)
    else:
        ax.axis["bottom"].set_visible(True)

    # xticks = []
    # xpos = []
    pos = 0
    xys = []
    ymax = -1e9
    ymin = 2e9
    for ndata, data in enumerate(datasets):
        if xdata is None:
            pos += gap2
            boffset = bar_width/2.0
        else:
            pos = xdata[ndata]
            boffset = 0
        xret.append(pos+boffset)
        if bar:
            ax.bar(pos, data.mean, width=bar_width, color=data.color,
                   edgecolor='k')
        if data.data is not None:
            ax.plot([pos+boffset for dat in data.data], 
                    data.data, 'o', ms=ms, mew=0, lw=1.0, alpha=0.5,
                    mfc='grey', color='grey')
            if paired:
                xys.append([[pos+boffset, dat] for dat in data.data])
            if np.max(data.data) > ymax:
                ymax = np.max(data.data)
            if np.min(data.data) < ymin:
                ymin = np.min(data.data)

        if data.mean+data.err > ymax:
            ymax = data.mean+data.err
        if data.mean-data.err < ymin:
            ymin = data.mean-data.err

        if data.err is not None:
            yerr_offset = data.err/2.0
            if data.mean < 0:
                sign = -1
            else:
                sign = 1
            if bar:
                fmt = None
                ymarker = data.mean+sign*yerr_offset
                yerr = sign*data.err/2.0
            else:
                fmt = '-_'
                ymarker = data.mean
                yerr = data.err

            erb = ax.errorbar(pos+boffset, ymarker,
                              yerr=yerr, fmt=fmt, ecolor='k', capsize=6,
                              ms=ms*2, mec='k', mfc='k')
            if data.err == 0:
                for erbs in erb[1]:
                    erbs.set_visible(False)
            if bar:
                erb[1][0].set_visible(False) # make lower error cap invisible

        ax.text(pos+boffset*1, labelpos, data.title, ha='center', va='top',
                rotation=0)

        if xdata is None:
            pos += bar_width+gap2

    if paired:
        for nxy in range(len(datasets[0].data)):
            ax.plot([xys[0][nxy][0], xys[1][nxy][0]],
                    [xys[0][nxy][1], xys[1][nxy][1]], '-k')

    if ymax > 0 and ymin > 0:
        ymin = 0
    if ymax < 0 and ymin < 0:
        ymax = 0

    if ylabel is not None:
        ax.set_ylabel(ylabel)
    if ylim is not None:
        ax.set_ylim(ylim)
    else:
        ax.set_ylim(ymin, ymax)
    if xdata is not None:
        ax.set_xlim(0, None)

    if not paired:
        if len(datasets) == 2:
            t, P = stats.ttest_ind(datasets[0].data, datasets[1].data)
            sys.stdout.write("t-test, %s vs %s, P=%.4f\n" % (
                datasets[0].title, datasets[1].title, P))
        elif len(datasets) > 2:
            F, P = stats.f_oneway(*[dataset.data for dataset in datasets])
            sys.stdout.write("ANOVA, %s" % datasets[0].title)
            for dataset in datasets[1:]:
                sys.stdout.write(" vs %s" % dataset.title)
            sys.stdout.write(", P=%.4f\n" % (P))

    return xret