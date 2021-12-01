import numpy as np
import json
import pprint
import random
import time
import torch
import torch.multiprocessing as mp
from models.nn.resnet import Resnet
from data.preprocess import Dataset
from importlib import import_module
from scripts.generate_maskrcnn import create_panorama, MaskRCNNDetector
from scripts.generate_maskrcnn import CustomImageLoader
from scripts.geometry_utils import calculate_angles

class Eval(object):

    # tokens
    STOP_TOKEN = "[stop]"
    SEQ_TOKEN = "<<seg>>"
    TERMINAL_TOKENS = [STOP_TOKEN, SEQ_TOKEN]

    def __init__(self, args, manager):
        # args and manager
        self.args = args
        self.manager = manager
        self.region_detector = MaskRCNNDetector(
                checkpoint_path="storage/models/vision/moca_maskrcnn/weight_maskrcnn.pt")
        self.region_detector.eval()
        self.image_loader = CustomImageLoader(min_size = self.args.frame_size, max_size=self.args.frame_size)

        # load splits
        with open(self.args.splits) as f:
            self.splits = json.load(f)
            pprint.pprint({k: len(v) for k, v in self.splits.items()})

        # load model
        print("Loading: ", self.args.model_path)
        M = import_module(self.args.model)
        #self.model, optimizer = M.Module.load(self.args.model_path)
        self.model = M.GoalConditionedTransformer.load(args, self.args.model_path)
        self.model.share_memory()
        # TODO: if we do conformal pred stuff we might need to turn off model.eval()
        self.model.eval()
        self.model.test_mode = True

        # updated args
        self.model.args.dout = self.args.model_path.replace(self.args.model_path.split('/')[-1], '')
        self.model.args.data = self.args.data if self.args.data else self.model.args.data

        # preprocess and save
        if args.preprocess:
            print("\nPreprocessing dataset and saving to %s folders ... This is will take a while. Do this once as required:" % self.model.args.pp_folder)
            self.model.args.fast_epoch = self.args.fast_epoch
            dataset = Dataset(self.model.args, self.model.vocab)
            dataset.preprocess_splits(self.splits)

        # load resnet
        args.visual_model = 'resnet18'
        self.resnet = Resnet(args, eval=True, share_memory=True, use_conv_feat=True)

        # gpu
        if self.args.gpu:
            self.model = self.model.to(torch.device('cuda'))
            self.region_detector = self.region_detector.to(torch.device("cuda"))

        # success and failure lists
        self.create_stats()

        # set random seed for shuffling
        random.seed(int(time.time()))

    def queue_tasks(self):
        '''
        create queue of trajectories to be evaluated
        '''
        task_queue = self.manager.Queue()
        files = self.splits[self.args.eval_split]

        # debugging: fast epoch
        if self.args.fast_epoch:
            files = files[:16]

        if self.args.shuffle:
            random.shuffle(files)
        for traj in files:
            task_queue.put(traj)
        return task_queue

    def spawn_threads(self):
        '''
        spawn multiple threads to run eval in parallel
        '''
        task_queue = self.queue_tasks()
        # start threads
        threads = []
        lock = self.manager.Lock()
        if self.args.num_threads == 1:
            self.run(self.model, self.resnet, self.image_loader, self.region_detector, task_queue, self.args, lock, self.successes, self.failures, self.results)
        else:
            for n in range(self.args.num_threads):
                thread = mp.Process(target=self.run, args=(self.model, self.resnet, self.image_loader, self.region_detector, task_queue, self.args, lock,
                                                        self.successes, self.failures, self.results))
                thread.start()
                threads.append(thread)

            for t in threads:
                t.join()

        # save
        self.save_results()

    @classmethod
    def setup_scene(cls, env, traj_data, r_idx, args, reward_type='dense'):
        '''
        intialize the scene and agent from the task info
        '''
        # scene setup
        scene_num = traj_data['scene']['scene_num']
        object_poses = traj_data['scene']['object_poses']
        dirty_and_empty = traj_data['scene']['dirty_and_empty']
        object_toggles = traj_data['scene']['object_toggles']

        scene_name = 'FloorPlan%d' % scene_num
        env.reset(scene_name)
        env.restore_scene(object_poses, object_toggles, dirty_and_empty)

        # initialize to start position
        env.step(dict(traj_data['scene']['init_action']))

        # print goal instr
        print("Task: %s" % (traj_data['turk_annotations']['anns'][r_idx]['task_desc']))

        # setup task for reward
        env.set_task(traj_data, args, reward_type=reward_type)

    @classmethod
    def run(cls, model, resnet, task_queue, args, lock, successes, failures):
        raise NotImplementedError()

    @classmethod
    def evaluate(cls, env, model, r_idx, resnet, traj_data, args, lock, successes, failures):
        raise NotImplementedError()

    def save_results(self):
        raise NotImplementedError()

    def create_stats(self):
        raise NotImplementedError()

    @classmethod
    def get_visual_features(cls, env, image_loader, region_detector, args,
                            cuda_device):
        # collect current robot view
        panorama_images, camera_infos = create_panorama(env, 0)

        images, sizes = image_loader(panorama_images, pack=True)

        # FasterRCNN feature extraction for the current frame
        #if cuda_device >= 0:
        images = images.to(cuda_device)

        detector_results = region_detector(images)

        object_features = []

        for i in range(len(detector_results)):
            num_boxes = args.panoramic_boxes[i]
            features = detector_results[i]["features"]
            coordinates = detector_results[i]["boxes"]
            class_probs = detector_results[i]["scores"]
            class_labels = detector_results[i]["labels"]
            masks = detector_results[i]["masks"]

            if coordinates.shape[0] > 0:
                coordinates = coordinates.cpu().numpy()
                center_coords = (coordinates[:, 0] + coordinates[:, 2]) // 2, (
                        coordinates[:, 1] + coordinates[:, 3]) // 2

                h_angle, v_angle = calculate_angles(
                    center_coords[0],
                    center_coords[1],
                    camera_infos[i]["h_view_angle"],
                    camera_infos[i]["v_view_angle"]
                )

                boxes_angles = np.stack([h_angle, v_angle], 1)
            else:
                boxes_angles = np.zeros((coordinates.shape[0], 2))
                coordinates = coordinates.cpu().numpy()

            box_features = features[:num_boxes]
            boxes_angles = boxes_angles[:num_boxes]
            boxes = coordinates[:num_boxes]
            masks = masks[:num_boxes]
            class_probs = class_probs[:num_boxes]
            class_labels = class_labels[:num_boxes]

            object_features.append(dict(
                box_features=box_features.cpu().numpy(),
                roi_angles=boxes_angles,
                boxes=boxes,
                masks=(masks > 0.5).cpu().numpy(),
                class_probs=class_probs.cpu().numpy(),
                class_labels=class_labels.cpu().numpy(),
                camera_info=camera_infos[i],
                num_objects=box_features.shape[0]
            ))

        return object_features


