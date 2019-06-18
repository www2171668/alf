# Copyright (c) 2019 Horizon Robotics. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import time

from absl import logging
import gin.tf
import tensorflow as tf

from tf_agents.eval import metric_utils
from tf_agents.utils import common as tfa_common

from alf.drivers.on_policy_driver import OnPolicyDriver
from alf.utils.metric_utils import eager_compute
from tf_agents.metrics import tf_metrics
from alf.utils.common import run_under_record_context, get_global_counter


@gin.configurable
def train(train_dir,
          env,
          algorithm,
          eval_env=None,
          random_seed=0,
          train_interval=20,
          num_steps_per_iter=10000,
          num_iterations=1000,
          use_tf_functions=True,
          summary_interval=50,
          summaries_flush_secs=1,
          eval_interval=10,
          num_eval_episodes=10,
          checkpoint_interval=1000,
          debug_summaries=False,
          summarize_grads_and_vars=False):
    """Perform on-policy training using OnPolicyDriver.

    NOTE: currently, for use_tf_function=False, all the summary names have an
    additional prefix "driver_loop", it's might be a bug of tf2. We'll see.

    Args:
        train_dir (str): directory for saving summary and checkpoints
        env (TFEnvironment): environment for training
        algorithm (OnPolicyAlgorithm): the training algorithm
        eval_env (TFEnvironment): environment for evaluating
        random_seed (int): random seed
        train_interval (int): update parameter every so many env.step().
        num_steps_per_iter (int): number of steps for one iteration. It is the
            total steps from all individual environment in the batch
            environment.
        use_tf_functions (bool): whether to use tf.function
        summary_interval (int): write summary every so many training steps (
            i.e. number of parameter updates)
        summaries_flush_secs (int): flush summary to disk every so many seconds.
        eval_interval (int): evaluate every so many iteration
        num_eval_episodes (int) : number of episodes for one evaluation
        checkpoint_interval (int): checkpoint every so many iterations
        debug_summaries (bool): A bool to gather debug summaries.
        summarize_grads_and_vars (bool): If True, gradient and network variable
            summaries will be written during training.
    """

    train_dir = os.path.expanduser(train_dir)
    eval_dir = os.path.join(os.path.dirname(train_dir), 'eval')

    eval_metrics = None
    eval_summary_writer = None
    if eval_env is not None:
        eval_metrics = [
            tf_metrics.AverageReturnMetric(buffer_size=num_eval_episodes),
            tf_metrics.AverageEpisodeLengthMetric(buffer_size=num_eval_episodes)
        ]
        eval_summary_writer = tf.summary.create_file_writer(
            eval_dir, flush_millis=summaries_flush_secs * 1000)

    def train_():
        tf.random.set_seed(random_seed)
        global_step = get_global_counter()

        driver = OnPolicyDriver(
            env=env,
            algorithm=algorithm,
            train_interval=train_interval,
            debug_summaries=debug_summaries,
            summarize_grads_and_vars=summarize_grads_and_vars)

        checkpointer = tfa_common.Checkpointer(
            ckpt_dir=os.path.join(train_dir, 'algorithm'),
            algorithm=algorithm,
            metrics=metric_utils.MetricsGroup(driver.get_metrics(), 'metrics'),
            global_step=global_step)
        checkpointer.initialize_or_restore()

        if use_tf_functions:
            driver.run = tf.function(driver.run)

        env.reset()
        time_step = driver.get_initial_time_step()
        policy_state = driver.get_initial_state()
        for iter in range(num_iterations):
            t0 = time.time()

            time_step, policy_state = driver.run(
                max_num_steps=num_steps_per_iter,
                time_step=time_step,
                policy_state=policy_state)

            logging.info('%s time=%.3f' % (iter, time.time() - t0))

            if (iter + 1) % checkpoint_interval == 0:
                checkpointer.save(global_step=global_step.numpy())

            if eval_env is not None and (iter + 1) % eval_interval == 0:
                with tf.summary.record_if(True):
                    eager_compute(metrics=eval_metrics,
                                  environment=eval_env,
                                  state_spec=algorithm.predict_state_spec,
                                  action_fn=algorithm.greedy_predict,
                                  num_episodes=num_eval_episodes,
                                  train_step=global_step,
                                  summary_writer=eval_summary_writer,
                                  summary_prefix="Metrics")
                    metric_utils.log_metrics(eval_metrics)

        checkpointer.save(global_step=global_step.numpy())

    run_under_record_context(
        func=train_,
        summary_dir=train_dir,
        summary_interval=summary_interval,
        flush_millis=summaries_flush_secs * 1000)


@gin.configurable
def play(train_dir,
         env,
         algorithm,
         checkpoint_name=None,
         greedy_predict=True,
         random_seed=0,
         num_steps=10000,
         sleep_time_per_step=0.01,
         use_tf_functions=True):
    """Play using the latest checkpoint under `train_dir`.

    Args:
        train_dir (str): same as the train_dir used for `train()`
        env (TFEnvironment): the environment
        algorithm (OnPolicyAlgorithm): the training algorithm
        checkpoint_name (str): name of the checkpoint (e.g. 'ckpt-12800`).
            If None, the latest checkpoint unber train_dir will be used.
        greedy_predict (bool): use greedy action for evaluation.
        random_seed (int): random seed
        num_steps (int): number of steps to play
        sleep_time_per_step (float): sleep so many seconds for each step
        use_tf_functions (bool): whether to use tf.function
    """
    train_dir = os.path.expanduser(train_dir)

    tf.random.set_seed(random_seed)
    global_step = get_global_counter()

    driver = OnPolicyDriver(
        env=env,
        algorithm=algorithm,
        training=False,
        greedy_predict=greedy_predict)

    ckpt_dir = os.path.join(train_dir, 'algorithm')
    checkpoint = tf.train.Checkpoint(
        algorithm=algorithm,
        metrics=metric_utils.MetricsGroup(driver.get_metrics(), 'metrics'),
        global_step=global_step)
    if checkpoint_name is not None:
        ckpt_path = os.path.join(ckpt_dir, checkpoint_name)
    else:
        ckpt_path = tf.train.latest_checkpoint(ckpt_dir)
    if ckpt_path is not None:
        logging.info("Restore from checkpoint %s" % ckpt_path)
        checkpoint.restore(ckpt_path)

    if use_tf_functions:
        driver.run = tf.function(driver.run)

    # pybullet_envs need to `render()` before reset() to enable rendering.
    env.pyenv.envs[0].render(mode='human')
    env.reset()
    time_step = driver.get_initial_time_step()
    policy_state = driver.get_initial_state()
    episode_reward = 0.
    episode_length = 0
    for _ in range(num_steps):
        time_step, policy_state = driver.run(
            max_num_steps=1, time_step=time_step, policy_state=policy_state)
        if time_step.is_last():
            logging.info("episode_length=%s episode_reward=%s" %
                         (episode_length, episode_reward))
            episode_reward = 0.
            episode_length = 0.
        else:
            episode_reward += float(time_step.reward)
            episode_length += 1
        env.pyenv.envs[0].render(mode='human')
        time.sleep(sleep_time_per_step)
    env.reset()