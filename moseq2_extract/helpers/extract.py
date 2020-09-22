'''

Extraction-helper utilities.
These functions are primarily called from inside the extract_wrapper() function.

'''

import os
import numpy as np
import pandas as pd
from tqdm.auto import tqdm
from moseq2_extract.extract.extract import extract_chunk
from moseq2_extract.io.video import load_movie_data, write_frames_preview
from moseq2_extract.extract.validation import count_nan_rows, count_missing_mouse_frames, count_stationary_frames

def write_extracted_chunk_to_h5(h5_file, results, config_data, scalars, frame_range, offset):
    '''

    Write extracted frames, frame masks, and scalars to an open h5 file.

    Parameters
    ----------
    h5_file (H5py.File): open results_00 h5 file to save data in.
    results (dict): extraction results dict.
    config_data (dict): dictionary containing extraction parameters (autogenerated)
    scalars (list): list of keys to scalar attribute values
    frame_range (range object): current chunk frame range
    offset (int): frame offset

    Returns
    -------
    config_data (dict): dictionary containing updated extraction validation parameter values
    '''

    scalar_dict = {}

    # Writing computed scalars to h5 file
    for scalar in scalars:
        h5_file[f'scalars/{scalar}'][frame_range] = results['scalars'][scalar][offset:]
        scalar_dict[scalar] = h5_file['scalars'][scalar][()]

    scalar_df = pd.DataFrame.from_dict(scalar_dict)
    config_data['corrupted_frames'] += count_nan_rows(scalar_df)
    config_data['corrupted_frames'] += count_missing_mouse_frames(scalar_df)

    config_data['motionless_frames'] += count_stationary_frames(scalar_df)

    # Writing frames and mask to h5
    h5_file['frames'][frame_range] = results['depth_frames'][offset:]
    h5_file['frames_mask'][frame_range] = results['mask_frames'][offset:]

    # Writing flip classifier results to h5
    if config_data['flip_classifier']:
        h5_file['metadata/extraction/flips'][frame_range] = results['flips'][offset:]

    return config_data

def process_extract_batches(input_file, config_data, bground_im, roi,
                            frame_batches, first_frame_idx, str_els,
                            output_dir, output_filename, scalars=None, h5_file=None, **kwargs):
    '''
    Given an open h5 file, which is used to store extraction results, and some pre-computed input session data points
    such as the background, ROI, etc. Compute extracted frames and save them to h5 files and avi files.
    Called from extract_wrapper()

    Parameters
    ----------
    h5file (h5py.File): opened h5 file to write extracted batches to
    input_file (str): path to depth file
    config_data (dict): dictionary containing extraction parameters (autogenerated)
    bground_im (2d numpy array):  r x c, background image
    roi (2d numpy array):  r x c, roi image
    scalars (list): list of keys to scalar attribute values
    frame_batches (list): list of batches of frames to serially process.
    first_frame_idx (int): index of starting frame.
    str_els (dict): dictionary containing OpenCV StructuringElements
    output_dir (str): path to output directory that contains the extracted data, e.g. (proc/).
    output_filename (str): name of h5 file containing extraction data, e.g. (results_00).

    Returns
    -------
    config_data (dict): dictionary containing updated extraction validation parameter values
    '''

    video_pipe = None
    config_data['tracking_init_mean'] = None
    config_data['tracking_init_cov'] = None
    config_data['corrupted_frames'] = 0
    config_data['motionless_frames'] = 0

    for i, frame_range in enumerate(tqdm(frame_batches, desc='Processing batches')):
        chunk_frames = [f + first_frame_idx for f in frame_range]
        raw_chunk = load_movie_data(input_file,
                                    chunk_frames,
                                    frame_dims=bground_im.shape[::-1],
                                    tar_object=config_data['tar'])

        # Get crop-rotated frame batch
        results = extract_chunk(**config_data,
                                **str_els,
                                chunk=raw_chunk,
                                roi=roi,
                                bground=bground_im
                                )

        if i > 0:
            offset = config_data['chunk_overlap']
        else:
            offset = 0

        if config_data['use_tracking_model']:
            # Thresholding and clipping EM-tracked frame mask data
            results['mask_frames'][results['depth_frames'] < config_data['min_height']] = config_data[
                'tracking_model_ll_clip']
            results['mask_frames'][results['mask_frames'] < config_data['tracking_model_ll_clip']] = config_data[
                'tracking_model_ll_clip']
            # Updating EM tracking estimators
            config_data['tracking_init_mean'] = results['parameters']['mean'][-(config_data['chunk_overlap'] + 1)]
            config_data['tracking_init_cov'] = results['parameters']['cov'][-(config_data['chunk_overlap'] + 1)]

        # Offsetting frame chunk by CLI parameter defined option: chunk_overlap
        frame_range = frame_range[offset:]

        if h5_file is not None:
            config_data = write_extracted_chunk_to_h5(h5_file, results, config_data, scalars, frame_range, offset)

        # Create empty array for output movie with filtered video and cropped mouse on the top left
        nframes, rows, cols = results['chunk'][offset:].shape
        output_movie = np.zeros((nframes, rows + config_data['crop_size'][0], cols + config_data['crop_size'][1]),
                                'uint16')

        # Populating array with filtered and cropped videos
        output_movie[:, :config_data['crop_size'][0], :config_data['crop_size'][1]] = results['depth_frames'][offset:]
        output_movie[:, config_data['crop_size'][0]:, config_data['crop_size'][1]:] = results['chunk'][offset:]

        # Writing frame batch to mp4 file
        video_pipe = write_frames_preview(
            os.path.join(output_dir, f'{output_filename}.mp4'), output_movie,
            pipe=video_pipe, close_pipe=False, fps=config_data['fps'],
            frame_range=[f + first_frame_idx for f in frame_range],
            depth_max=config_data['max_height'], depth_min=config_data['min_height'],
            progress_bar=config_data.get('progress_bar', False))

    # Check if video is done writing. If not, wait.
    if video_pipe is not None:
        video_pipe.stdin.close()
        video_pipe.wait()

    return config_data

def run_local_extract(to_extract, config_file, skip_extracted=False):
    '''
    Runs the extract command on given list of sessions to extract on a local platform.
    This function is meant for the GUI interface to utilize the moseq2-batch extract functionality.

    Parameters
    ----------
    to_extract (list): list of paths to files to extract
    config_file (str): path to configuration file containing pre-configured extract and ROI
    skip_extracted (bool): Whether to skip already extracted session.

    Returns
    -------
    None
    '''
    from moseq2_extract.gui import extract_command

    for i, ext in tqdm(enumerate(to_extract), total=len(to_extract), desc='Extracting Sessions'):
        try:
            extract_command(ext, to_extract[i].replace(ext, 'proc/'), config_file=config_file, skip=skip_extracted)
        except Exception as e:
            print('Unexpected error:', e)
            print('could not extract', to_extract[i])
