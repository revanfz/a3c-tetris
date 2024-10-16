import os
os.environ["OMP_NUM_THREADS"] = "1"

import sys
import time
import torch
import optuna
import pickle
import logging
import torch.nn as nn
import torch.nn.functional as F
import torch.multiprocessing as mp

from tqdm import tqdm
from typing import Union
from model import UNREAL
from replay_buffer import ReplayBuffer
from torch.distributions import Categorical
from multiprocessing.synchronize import Event
from optimizer import SharedAdam, SharedRMSprop
from multiprocessing.sharedctypes import Synchronized
from utils import ensure_share_grads, make_env, preprocessing


def print_best_trial(trials, index: int, metric_name: str):
    best_trial = max(trials, key=lambda t: t.values[index])
    print(f"\nTrial with highest {metric_name}:")
    print(f"\tnumber: {best_trial.number}")
    print(f"\tparams: {best_trial.params}")
    print(f"\tvalues: {best_trial.values}")
    print()


def train(
    rank: int,
    params: dict,
    global_model: UNREAL,
    optimizer: Union[SharedAdam, SharedRMSprop],
    global_steps: Synchronized,
    global_rewards: Synchronized,
    stop_event: Event,
    trial: optuna.Trial,
) -> None:
    try:
        device = torch.device("cpu")

        torch.manual_seed(42 + rank)
        env = make_env(resize=None, grayscale=False, framestack=None)

        local_model = UNREAL(
            n_inputs=(84, 84, 3),
            n_actions=env.action_space.n,
            hidden_size=params["hidden_size"],
            beta=params["beta"],
            device=device,
        )
        local_model.train()
        experience_replay = ReplayBuffer(2000)

        done = True
        num_games = 0

        while not stop_event.is_set():
            optimizer.zero_grad()
            local_model.load_state_dict(global_model.state_dict())

            for _ in range(params["unroll_steps"]):
                with global_steps.get_lock():
                    global_steps.value += 1
                if done:
                    state, info = env.reset()
                    state = preprocessing(state)
                    prev_action = torch.zeros(1, env.action_space.n).to(device)
                    prev_reward = torch.zeros(1, 1).to(device)
                    hx = torch.zeros(1, params["hidden_size"]).to(device)
                    cx = torch.zeros(1, params["hidden_size"]).to(device)
                    prev_lines = 0
                    if not rank and num_games:
                        trial.report(global_rewards.value, step=num_games)
                        
                    num_games += 1
                else:
                    hx = hx.data
                    cx = cx.data

                state_tensor = torch.FloatTensor(state).unsqueeze(0).to(device)
                policy, _, _, hx, cx = local_model(
                    state_tensor, prev_action, prev_reward, (hx, cx)
                )

                dist = Categorical(policy)
                action = dist.sample().detach()

                next_state, reward, done, _, info = env.step(action.item())
                if info["number_of_lines"] > prev_lines:
                    reward += 10 * (info["number_of_lines"] - prev_lines)
                    prev_lines = info["number_of_lines"]
                with global_rewards.get_lock():
                    global_rewards.value += reward
                next_state = preprocessing(next_state)

                experience_replay.store(
                    state,
                    prev_action.argmax().item(),
                    prev_reward.item(),
                    next_state,
                    action.item(),
                    reward,
                    done,
                )

                prev_action = F.one_hot(action, num_classes=env.action_space.n).to(
                    device
                )
                prev_reward = torch.FloatTensor([reward]).unsqueeze(0).to(device)
                state = next_state

            # Hitung loss A3C
            # 1. Sampel replay buffer secara sekuensial
            states, prev_actions, prev_rewards, _, _, _, dones = experience_replay.sample(
                params["unroll_steps"], base=True
            )
            # 2. Hitung loss actor dan critic
            policy_loss, value_loss, _ = local_model.a3c_loss(
                states, prev_rewards, prev_actions, dones
            )
            # 3. Jumlahkan loss dengan mengurangi nilai critic loss
            a3c_loss = policy_loss + value_loss

            # Hitung Loss Pixel Control dan Feature Control
            # 1.  Sampling replay buffer secara random
            states, prev_actions, prev_rewards, next_states, next_actions, next_rewards, dones = experience_replay.sample(
                params["unroll_steps"]
            )
            # 2a. Hitung loss Pixel Control
            aux_control_loss = local_model.control_loss(
                states, prev_actions, prev_rewards, next_states, next_actions, next_rewards, dones
            )  
            # 2b. Hitung loss Value Replay
            vr_loss = local_model.vr_loss(states, prev_actions, prev_rewards, dones)

            # Hitung Loss Reward Pedictions
            # 1. Sampel 3 frame dengan pleuang rewarding state = 0.5
            states, prev_rewards, next_rewards = experience_replay.sample_rp()
            # 2. Hitung loss reward prediction
            rp_loss = local_model.rp_loss(states, prev_rewards, next_rewards[-1])

            # Penjumlahan loss a3c, pixel control, value replay dan reward prediction
            total_loss = (
                a3c_loss + vr_loss + params["task_weight"] * aux_control_loss + rp_loss
            )

            total_loss.backward()
            nn.utils.clip_grad_norm_(local_model.parameters(), 10)
            ensure_share_grads(
                local_model=local_model, global_model=global_model, device=device
            )
            optimizer.step()
                
        print(f"Agent {rank} training process finished.")

    except (KeyboardInterrupt, torch.multiprocessing.ProcessError) as e:
        print(e)

    finally:
        # with global_block_placed.get_lock():
        #     global_block_placed.value += (block_placed + episode_blocks) / (num_games + 1)
        env.close()


def objective(trial: optuna.Trial):
    try:
        env = make_env(resize=84, grayscale=False, framestack=None)

        params = {
            "lr": trial.suggest_float("learning rate", 1e-4, 5e-3, log=True),
            "task_weight": trial.suggest_float("pc weight", 0.01, 0.1, log=True),
            "beta": trial.suggest_float("entropy coefficient", 5e-4, 1e-2, log=True),
            "gamma": trial.suggest_float("dicount factor", 0.9, 0.999, log=True),
            "optimizer": "RMSProp",
            "device": torch.device("cuda"),
            "hidden_size": 256,
            "n_actions": env.action_space.n,
            "model_path": "trained_models",
            "input_shape": env.observation_space.shape,
            "unroll_steps": 20,
            "test_episodes": 5,
            "num_agents": 1,
        }

        global_model = UNREAL(
            n_inputs=params["input_shape"],
            n_actions=params["n_actions"],
            hidden_size=params["hidden_size"],
            beta=params["beta"],
            gamma=params["gamma"],
            device=torch.device("cpu"),
        )
        global_model.share_memory()

        if params["optimizer"] == "Adam":
            optimizer = SharedAdam(global_model.parameters(), lr=params["lr"])
        elif params["optimizer"] == "RMSProp":
            optimizer = SharedRMSprop(global_model.parameters(), lr=params["lr"])
        optimizer.share_memory()

        processes = []
        stop_event = mp.Event()
        global_rewards = mp.Value("f", 0.0)
        global_steps = mp.Value("i", 0)
        start_time = time.time()

        for rank in range(params["num_agents"]):
            p = mp.Process(
                target=train,
                args=(
                    rank,
                    params,
                    global_model,
                    optimizer,
                    global_steps,
                    global_rewards,
                    stop_event,
                    trial,
                ),
            )
            p.start()
            processes.append(p)

        train_time = 3600
        with tqdm(total=train_time, desc=f"Trial {trial.number}", unit="s") as pbar:
            pbar.update(int(time.time() - start_time))
            while time.time() - start_time < train_time:
                if all(not p.is_alive() for p in processes):
                    break
                time.sleep(1)
                pbar.update(1)

                # Cek apakah trial harus di-prune
                if trial.should_prune():
                    stop_event.set()
                    break

        stop_event.set()

        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()

        env.close()

        # mean_lines = total_lines / num_test
        # mean_blocks = total_blocks / num_test
        # mean_eps_length = episode_length / num_test
        # mean_blocks = global_block_placed.value / params["num_agents"]
        # return mean_blocks
        # , mean_lines, mean_eps_length
        mean_rewards = global_rewards.value / global_steps.value
        return mean_rewards


    except KeyboardInterrupt:
        raise KeyboardInterrupt("Tuning dihentikan.")

    except Exception as e:
        raise Exception(f"Error {e}")


if __name__ == "__main__":
    try:
        optuna.logging.get_logger("optuna").addHandler(
            logging.StreamHandler(sys.stdout)
        )
        storage = optuna.storages.RDBStorage(
            url="sqlite:///tuning/tuning_a3c.db",
            engine_kwargs={"connect_args": {"timeout": 30}},
        )
        study = optuna.create_study(
            study_name="final",
            storage=storage,
            load_if_exists=True,
            directions=["maximize"]
        )

        if os.path.isfile("./tuning/sampler.pkl"):
            restored_sampler = pickle.load(open("tuning/sampler.pkl", "rb"))
            study.sampler = restored_sampler

        completed_trials = len(
            [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
        )
        n_trials = 15

        study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

        print("Number of finished trials: ", len(study.trials))

        print(f"Number of trials on the Pareto front: {len(study.best_trials)}")

        metrics = [
            (0, "rewards"),
            # (0, "blocks placed"),
            # (1, "lines cleared"),
            # (2, "episodes length"),
        ]

        for index, metric_name in metrics:
            print_best_trial(study.best_trials, index, metric_name)

        print("Tuning selesai.")

    except (KeyboardInterrupt, optuna.exceptions.OptunaError, Exception) as e:
        print(f"Error: {e}")
        print("Tuning berhenti.")

    finally:
        with open("./tuning/sampler.pkl", "wb") as fout:
            pickle.dump(study.sampler, fout)
        print("Proses tuning dihentikan")
