import argparse
import datetime
import os
import time

import gym
import numpy as np

from research.datasets import ReplayBuffer
from research.utils.config import Config
from research.utils.evaluate import EvalMetricTracker

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=str, required=True)
    parser.add_argument("--num-ep", type=int, default=np.inf)
    parser.add_argument("--num-steps", type=int, default=np.inf)
    parser.add_argument(
        "--shard", action="store_true", default=False, help="Whether or not to shard the dataset into episodes."
    )
    parser.add_argument("--noise", type=float, default=0.0, help="Gaussian noise std.")
    parser.add_argument("--random-percent", type=float, default=0.0, help="percent of dataset to be purely random.")
    parser.add_argument("--checkpoint", type=str, default=None, help="Checkpoint to load")
    parser.add_argument("--device", type=str, default="auto")
    parser.add_argument(
        "--override",
        metavar="KEY=VALUE",
        nargs="+",
        default=[],
        help="Set kv pairs used as args for the entry point script.",
    )
    args = parser.parse_args()

    assert not (args.num_steps == np.inf and args.num_ep == np.inf), "Must set one of num-steps and num-ep"
    assert not (args.num_steps != np.inf and args.num_ep != np.inf), "Cannot set both num-steps and num-ep"
    assert args.random_percent <= 1.0 and args.random_percent >= 0.0, "Invalid random-percent"

    if os.path.exists(args.path):
        print("[research] Warning: saving dataset to an existing directory.")
    os.makedirs(args.path, exist_ok=True)

    # Load the config
    config = Config.load(os.path.dirname(args.checkpoint) if args.checkpoint.endswith(".pt") else args.checkpoint)
    config["checkpoint"] = None  # Set checkpoint to None

    # Overrides
    print("Overrides:")
    for override in args.override:
        print(override)

    for override in args.override:
        items = override.split("=")
        key, value = items[0].strip(), "=".join(items[1:])
        # Progress down the config path (seperated by '.') until we reach the final value to override.
        config_path = key.split(".")
        config_dict = config
        while len(config_path) > 1:
            config_dict = config_dict[config_path[0]]
            config_path.pop(0)
        config_dict[config_path[0]] = value

    # Parse the config
    config = config.parse()

    # Get the environment
    env = config.get_train_env_fn()()
    if env is None:
        env = config.get_eval_env_fn()()

    if args.random_percent < 1.0:
        assert args.checkpoint.endswith(".pt"), "Did not specify checkpoint file."
        model = config.get_model(
            observation_space=env.observation_space, action_space=env.action_space, device=args.device
        )
        metadata = model.load(args.checkpoint)
    else:
        model = None

    # Calculate the replay buffer capacity
    if args.num_ep < np.inf and not args.shard:
        try:
            max_ep_steps = env._max_episode_steps
        except AttributeError:
            max_ep_steps = env.unwrapped._max_episode_steps
        capacity = (max_ep_steps + 2) * args.num_ep
    elif not args.shard:
        capacity = args.num_steps
    else:
        capacity = 2

    # Init the replay buffer.
    replay_buffer = ReplayBuffer(
        env.observation_space, env.action_space, capacity=capacity, cleanup=not args.shard, distributed=args.shard
    )

    print(replay_buffer._storage["done"].dtype)

    # Track data collection
    num_steps = 0
    num_ep = 0
    finished_data_collection = False
    # Episode metrics
    metric_tracker = EvalMetricTracker()
    start_time = time.time()

    while not finished_data_collection:
        # Determine if we should use random actions or not.
        progress = num_ep / args.num_ep if args.num_ep != np.inf else num_steps / args.num_steps
        use_random_actions = progress < args.random_percent

        # Collect an episode
        done = False
        ep_length = 0
        obs = env.reset()
        metric_tracker.reset()
        replay_buffer.add(obs=obs)
        while not done:
            if use_random_actions:
                action = env.action_space.sample()
            else:
                action = model.predict(dict(obs=obs))
                if args.noise > 0:
                    assert isinstance(env.action_space, gym.spaces.Box)
                    action = action + args.noise * np.random.randn(*action.shape)
                    # Step the environment with the predicted action
                    env_action = np.clip(action, env.action_space.low, env.action_space.high)

            obs, reward, done, info = env.step(action)
            metric_tracker.step(reward, info)
            ep_length += 1

            # Determine the discount factor.
            if "discount" in info:
                discount = info["discount"]
            elif hasattr(env, "_max_episode_steps") and ep_length == env._max_episode_steps:
                discount = 1.0
            else:
                discount = 1 - float(done)

            # Store the consequences.
            replay_buffer.add(obs=obs, action=action, reward=reward, done=done, discount=discount)
            num_steps += 1

        num_ep += 1
        # Determine if we should stop data collection
        finished_data_collection = num_steps >= args.num_steps or num_ep >= args.num_ep

    end_time = time.time()
    print("Finished", num_ep, "episodes in", num_steps, "steps.")
    print("It took", (end_time - start_time) / num_steps, "seconds per step")

    replay_buffer.save(args.path)

    # Write the metrics
    metrics = metric_tracker.export()
    dt = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    print("Metrics:")
    print(metrics)
    with open(os.path.join(args.path, "metrics.txt"), "a") as f:
        f.write("Collected data: " + str(dt) + "\n")
        for k, v in metrics.items():
            f.write(k + ": " + str(v) + "\n")
