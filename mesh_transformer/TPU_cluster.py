import itertools
import json
import time

import ray

from typing import Callable
import numpy as np

from mesh_transformer.train_actor import NetworkRunner
from google.cloud import storage
from smart_open import open


class TPUCluster:
    def __init__(self,
                 mesh_shape,
                 node_count,
                 model: Callable):
        assert ray.is_initialized()  # needs a valid ray cluster to start
        self.nodes = []
        self.node_count = node_count
        self.dp, self.mp = mesh_shape

        for i in range(node_count):
            self.nodes.append(NetworkRunner.options(max_concurrency=2).remote(mesh_shape, model))

        for n in self.nodes:
            n.run.remote()

    def train(self, data):
        data_chunks = np.array_split(data, len(self.nodes), axis=1)

        res = []
        for n, d in zip(self.nodes, data_chunks):
            res.append(n.train.remote({
                "obs": d[:, :, :-1],
                "target": d[:, :, 1:],
            }))

        return np.array(ray.get(res)).mean()

    def eval(self, data):
        data_chunks = np.array_split(data, len(self.nodes), axis=0)

        res = []
        for n, d in zip(self.nodes, data_chunks):
            res.append(n.eval.remote({
                "obs": d[:, :-1],
                "target": d[:, 1:],
            }))

        return np.array(ray.get(res)).mean()

    def load(self, bucket, path):
        with open(f"gs://{bucket}/{path}/meta.json", "r") as f:
            meta = json.load(f)

        ckpt_step = meta["checkpoints"][-1]

        # do replicated checkpoint reading
        start = time.time()
        res = []
        for node in self.nodes:
            res.append(node.load_ckpt.remote(f"gs://{bucket}/{path}/step_{ckpt_step}/"))

        # make sure they all read from the same checkpoint
        step = np.array(ray.get(res))
        assert (step[0] == step).all()
        step = int(step[0])

        print(f"Checkpoint@step{step} restored in {time.time() - start:.06}s")
        return step, meta["aux"][str(ckpt_step)]

    def save(self, step, bucket, path, aux=None, init=False, overwrite=False, keep_n=3):
        assert path
        client = storage.Client()

        if aux is None:
            aux = {}

        if init:
            # check existing checkpoint folder does not exist, and delete it if it does
            for blob in client.list_blobs(bucket, prefix=f"{path}/"):
                assert overwrite
                # print(f"deleting {blob.name}")
                assert path in blob.name
                blob.delete()

            # create metadata file
            with open(f"gs://{bucket}/{path}/meta.json", "w") as f:
                json.dump({
                    "step": 0,
                    "checkpoints": [],
                    "aux": {}
                }, f)

        # do sharded checkpoint writing
        start = time.time()
        res = []
        for shard_id, node in zip(range(self.mp), itertools.cycle(self.nodes)):
            res.append(node.write_ckpt.remote(f"gs://{bucket}/{path}/step_{step}/", shard_id))
        ray.get(res)
        print(f"Wrote checkpoint in {time.time() - start:.06}s")

        with open(f"gs://{bucket}/{path}/meta.json", "r") as f:
            meta = json.load(f)

        meta["step"] = step
        meta["checkpoints"].append(step)
        all_aux = meta.get("aux", {})

        while len(meta["checkpoints"]) > keep_n:
            ckpt_to_delete = meta["checkpoints"].pop(0)

            try:
                del all_aux[str(ckpt_to_delete)]
            except:
                print(f"failed to delete the aux state for {step}")

            print(f"deleting checkpoint {ckpt_to_delete}")
            for blob in client.list_blobs(bucket, prefix=f"{path}/step_{ckpt_to_delete}/"):
                # print(f"deleting {blob.name}")
                assert path in blob.name
                blob.delete()

        all_aux[step] = aux
        meta["aux"] = all_aux

        with open(f"gs://{bucket}/{path}/meta.json", "w") as f:
            json.dump(meta, f)
