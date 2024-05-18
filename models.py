import copy
import torch
from torch import nn
import numpy as np
from PIL import ImageColor, Image, ImageDraw, ImageFont

import networks
import tools

to_np = lambda x: x.detach().cpu().numpy()


def symlog(x):
    return torch.sign(x) * torch.log(torch.abs(x) + 1.0)


def symexp(x):
    return torch.sign(x) * (torch.exp(torch.abs(x)) - 1.0)


class RewardEMA(object):
    """running mean and std"""

    def __init__(self, device, alpha=1e-2):
        self.device = device
        self.scale = torch.zeros((1,)).to(device)
        self.alpha = alpha
        self.range = torch.tensor([0.05, 0.95]).to(device)

    def __call__(self, x):
        flat_x = torch.flatten(x.detach())
        x_quantile = torch.quantile(input=flat_x, q=self.range)
        scale = x_quantile[1] - x_quantile[0]
        new_scale = self.alpha * scale + (1 - self.alpha) * self.scale
        self.scale = new_scale
        return x / torch.clip(self.scale, min=1.0)


class WorldModel(nn.Module):
    def __init__(self, step, config):
        super(WorldModel, self).__init__()
        self._step = step
        self._use_amp = True if config.precision == 16 else False
        self._config = config
        self.encoder = networks.ConvEncoder(
            config.grayscale,
            config.cnn_depth,
            config.act,
            config.norm,
            config.encoder_kernels,
        )
        if config.size[0] == 64 and config.size[1] == 64:
            embed_size = (
                (64 // 2 ** (len(config.encoder_kernels))) ** 2
                * config.cnn_depth
                * 2 ** (len(config.encoder_kernels) - 1)
            )
        else:
            raise NotImplemented(f"{config.size} is not applicable now")
        self.dynamics = networks.RSSM(
            config.dyn_stoch,
            config.dyn_deter,
            config.dyn_hidden,
            config.dyn_input_layers,
            config.dyn_output_layers,
            config.dyn_rec_depth,
            config.dyn_shared,
            config.dyn_discrete,
            config.act,
            config.norm,
            config.dyn_mean_act,
            config.dyn_std_act,
            config.dyn_temp_post,
            config.dyn_min_std,
            config.dyn_cell,
            config.unimix_ratio,
            config.num_actions,
            embed_size,
            config.device,
        )
        self.heads = nn.ModuleDict()
        channels = 1 if config.grayscale else 3
        shape = (channels,) + config.size
        if config.dyn_discrete:
            feat_size = config.dyn_stoch * config.dyn_discrete + config.dyn_deter
        else:
            feat_size = config.dyn_stoch + config.dyn_deter
        self.heads["image"] = networks.ConvDecoder(
            feat_size,  # pytorch version
            config.cnn_depth,
            config.act,
            config.norm,
            shape,
            config.decoder_kernels,
        )
        if config.reward_head == "twohot":
            self.heads["reward"] = networks.DenseHead(
                feat_size,  # pytorch version
                (255,),
                config.reward_layers,
                config.units,
                config.act,
                config.norm,
                dist=config.reward_head,
            )
        else:
            self.heads["reward"] = networks.DenseHead(
                feat_size,  # pytorch version
                [],
                config.reward_layers,
                config.units,
                config.act,
                config.norm,
                dist=config.reward_head,
            )
        # added this
        self.heads["reward"].apply(tools.weight_init)
        if config.pred_discount:
            self.heads["discount"] = networks.DenseHead(
                feat_size,  # pytorch version
                [],
                config.discount_layers,
                config.units,
                config.act,
                config.norm,
                dist="binary",
            )
        for name in config.grad_heads:
            assert name in self.heads, name
        self._model_opt = tools.Optimizer(
            "model",
            self.parameters(),
            config.model_lr,
            config.opt_eps,
            config.grad_clip,
            config.weight_decay,
            opt=config.opt,
            use_amp=self._use_amp,
        )
        self._scales = dict(reward=config.reward_scale, discount=config.discount_scale)

    def _train(self, data):
        # action (batch_size, batch_length, act_dim)
        # image (batch_size, batch_length, h, w, ch)
        # reward (batch_size, batch_length)
        # discount (batch_size, batch_length)
        data = self.preprocess(data)

        with tools.RequiresGrad(self):
            with torch.cuda.amp.autocast(self._use_amp):
                embed = self.encoder(data)
                post, prior = self.dynamics.observe(embed, data["action"])
                kl_free = tools.schedule(self._config.kl_free, self._step)
                kl_lscale = tools.schedule(self._config.kl_lscale, self._step)
                kl_rscale = tools.schedule(self._config.kl_rscale, self._step)
                kl_loss, kl_value, loss_lhs, loss_rhs = self.dynamics.kl_loss(
                    post, prior, self._config.kl_forward, kl_free, kl_lscale, kl_rscale
                )
                losses = {}
                likes = {}
                for name, head in self.heads.items():
                    grad_head = name in self._config.grad_heads
                    feat = self.dynamics.get_feat(post)
                    feat = feat if grad_head else feat.detach()
                    pred = head(feat)
                    # if name == 'image':
                    #   losses[name] = torch.nn.functional.mse_loss(pred.mode(), data[name], 'sum')
                    like = pred.log_prob(data[name])
                    likes[name] = like
                    losses[name] = -torch.mean(like) * self._scales.get(name, 1.0)
                model_loss = sum(losses.values()) + kl_loss
            metrics = self._model_opt(model_loss, self.parameters())

        metrics.update({f"{name}_loss": to_np(loss) for name, loss in losses.items()})
        metrics["kl_free"] = kl_free
        metrics["kl_lscale"] = kl_lscale
        metrics["kl_rscale"] = kl_rscale
        metrics["loss_lhs"] = to_np(loss_lhs)
        metrics["loss_rhs"] = to_np(loss_rhs)
        metrics["kl"] = to_np(torch.mean(kl_value))
        with torch.cuda.amp.autocast(self._use_amp):
            metrics["prior_ent"] = to_np(
                torch.mean(self.dynamics.get_dist(prior).entropy())
            )
            metrics["post_ent"] = to_np(
                torch.mean(self.dynamics.get_dist(post).entropy())
            )
            context = dict(
                embed=embed,
                feat=self.dynamics.get_feat(post),
                kl=kl_value,
                postent=self.dynamics.get_dist(post).entropy(),
            )
        post = {k: v.detach() for k, v in post.items()}
        return post, context, metrics

    def preprocess(self, obs):
        obs = obs.copy()
        if self._config.obs_trans == "normalize":
            obs["image"] = torch.Tensor(obs["image"]) / 255.0 - 0.5
        elif self._config.obs_trans == "identity":
            obs["image"] = torch.Tensor(obs["image"])
        elif self._config.obs_trans == "symlog":
            obs["image"] = symlog(torch.Tensor(obs["image"]))
        else:
            raise NotImplemented(f"{self._config.reward_trans} is not implemented")
        if self._config.reward_trans == "tanh":
            # (batch_size, batch_length) -> (batch_size, batch_length, 1)
            obs["reward"] = torch.tanh(torch.Tensor(obs["reward"])).unsqueeze(-1)
        elif self._config.reward_trans == "identity":
            # (batch_size, batch_length) -> (batch_size, batch_length, 1)
            obs["reward"] = torch.Tensor(obs["reward"]).unsqueeze(-1)
        elif self._config.reward_trans == "symlog":
            obs["reward"] = symlog(torch.Tensor(obs["reward"])).unsqueeze(-1)
        else:
            raise NotImplemented(f"{self._config.reward_trans} is not implemented")
        if "discount" in obs:
            obs["discount"] *= self._config.discount
            # (batch_size, batch_length) -> (batch_size, batch_length, 1)
            obs["discount"] = torch.Tensor(obs["discount"]).unsqueeze(-1)
        obs = {k: torch.Tensor(v).to(self._config.device) for k, v in obs.items()}
        return obs

    def video_pred(self, data):
        data = self.preprocess(data)
        embed = self.encoder(data)

        states, _ = self.dynamics.observe(embed[:6, :5], data["action"][:6, :5])
        recon = self.heads["image"](self.dynamics.get_feat(states)).mode()[:6]
        reward_post = self.heads["reward"](self.dynamics.get_feat(states)).mode()[:6]
        init = {k: v[:, -1] for k, v in states.items()}
        prior = self.dynamics.imagine(data["action"][:6, 5:], init)
        openl = self.heads["image"](self.dynamics.get_feat(prior)).mode()
        reward_prior = self.heads["reward"](self.dynamics.get_feat(prior)).mode()
        # observed image is given until 5 steps
        model = torch.cat([recon[:, :5], openl], 1)
        if self._config.obs_trans == "normalize":
            truth = data["image"][:6] + 0.5
            model += 0.5
        elif self._config.obs_trans == "symlog":
            truth = symexp(data["image"][:6]) / 255.0
            model = symexp(model) / 255.0
        error = (model - truth + 1) / 2

        return torch.cat([truth, model, error], 2)


class ImagBehavior(nn.Module):
    def __init__(self, config, world_model, stop_grad_actor=True, reward=None):
        super(ImagBehavior, self).__init__()
        self._use_amp = True if config.precision == 16 else False
        self._config = config
        self._world_model = world_model
        self._stop_grad_actor = stop_grad_actor
        self._reward = reward
        if config.dyn_discrete:
            feat_size = config.dyn_stoch * config.dyn_discrete + config.dyn_deter
        else:
            feat_size = config.dyn_stoch + config.dyn_deter
        self.actor = networks.ActionHead(
            feat_size,  # pytorch version
            config.num_actions,
            config.actor_layers,
            config.units,
            config.act,
            config.norm,
            config.actor_dist,
            config.actor_init_std,
            config.actor_min_std,
            config.actor_dist,
            config.actor_temp,
            config.actor_outscale,
        )  # action_dist -> action_disc?
        if config.value_head == "twohot":
            self.value = networks.DenseHead(
                feat_size,  # pytorch version
                (255,),
                config.value_layers,
                config.units,
                config.act,
                config.norm,
                config.value_head,
            )
        else:
            self.value = networks.DenseHead(
                feat_size,  # pytorch version
                [],
                config.value_layers,
                config.units,
                config.act,
                config.norm,
                config.value_head,
            )
        self.value.apply(tools.weight_init)
        if config.slow_value_target or config.slow_actor_target:
            self._slow_value = copy.deepcopy(self.value)
            self._updates = 0
        kw = dict(wd=config.weight_decay, opt=config.opt, use_amp=self._use_amp)
        self._actor_opt = tools.Optimizer(
            "actor",
            self.actor.parameters(),
            config.actor_lr,
            config.ac_opt_eps,
            config.actor_grad_clip,
            **kw,
        )
        self._value_opt = tools.Optimizer(
            "value",
            self.value.parameters(),
            config.value_lr,
            config.ac_opt_eps,
            config.value_grad_clip,
            **kw,
        )
        if self._config.reward_EMA:
            self.reward_ema = RewardEMA(device=self._config.device)

    def _train(
        self,
        start,
        objective=None,
        action=None,
        reward=None,
        imagine=None,
        tape=None,
        repeats=None,
    ):
        objective = objective or self._reward
        self._update_slow_target()
        metrics = {}

        with tools.RequiresGrad(self.actor):
            with torch.cuda.amp.autocast(self._use_amp):
                imag_feat, imag_state, imag_action = self._imagine(
                    start, self.actor, self._config.imag_horizon, repeats
                )
                reward = objective(imag_feat, imag_state, imag_action)
                if self._config.reward_trans == "symlog":
                    # rescale predicted reward by head['reward']
                    reward = symexp(reward)
                actor_ent = self.actor(imag_feat).entropy()
                state_ent = self._world_model.dynamics.get_dist(imag_state).entropy()
                # this target is not scaled
                # slow is flag to indicate whether slow_target is used for lambda-return
                target, weights = self._compute_target(
                    imag_feat,
                    imag_state,
                    imag_action,
                    reward,
                    actor_ent,
                    state_ent,
                    self._config.slow_actor_target,
                )
                actor_loss, mets = self._compute_actor_loss(
                    imag_feat,
                    imag_state,
                    imag_action,
                    target,
                    actor_ent,
                    state_ent,
                    weights,
                )
                metrics.update(mets)
                if self._config.slow_value_target != self._config.slow_actor_target:
                    target, weights = self._compute_target(
                        imag_feat,
                        imag_state,
                        imag_action,
                        reward,
                        actor_ent,
                        state_ent,
                        self._config.slow_value_target,
                    )
                value_input = imag_feat

        with tools.RequiresGrad(self.value):
            with torch.cuda.amp.autocast(self._use_amp):
                value = self.value(value_input[:-1].detach())
                target = torch.stack(target, dim=1)
                # only critic target is processed using symlog(not actor)
                if self._config.critic_trans == "symlog":
                    metrics["unscaled_target_mean"] = to_np(torch.mean(target))
                    target = symlog(target)
                # (time, batch, 1), (time, batch, 1) -> (time, batch)
                value_loss = -value.log_prob(target.detach())
                if self._config.value_decay:
                    value_loss += self._config.value_decay * value.mode()
                # (time, batch, 1), (time, batch, 1) -> (1,)
                value_loss = torch.mean(weights[:-1] * value_loss[:, :, None])

        metrics["value_mean"] = to_np(torch.mean(value.mode()))
        metrics["value_max"] = to_np(torch.max(value.mode()))
        metrics["value_min"] = to_np(torch.min(value.mode()))
        metrics["value_std"] = to_np(torch.std(value.mode()))
        metrics["target_mean"] = to_np(torch.mean(target))
        metrics["reward_mean"] = to_np(torch.mean(reward))
        metrics["reward_std"] = to_np(torch.std(reward))
        metrics["actor_ent"] = to_np(torch.mean(actor_ent))
        with tools.RequiresGrad(self):
            metrics.update(self._actor_opt(actor_loss, self.actor.parameters()))
            metrics.update(self._value_opt(value_loss, self.value.parameters()))
        return imag_feat, imag_state, imag_action, weights, metrics

    def _imagine(self, start, policy, horizon, repeats=None):
        dynamics = self._world_model.dynamics
        if repeats:
            raise NotImplemented("repeats is not implemented in this version")
        flatten = lambda x: x.reshape([-1] + list(x.shape[2:]))
        start = {k: flatten(v) for k, v in start.items()}

        def step(prev, _):
            state, _, _ = prev
            feat = dynamics.get_feat(state)
            inp = feat.detach() if self._stop_grad_actor else feat
            action = policy(inp).sample()
            succ = dynamics.img_step(state, action, sample=self._config.imag_sample)
            return succ, feat, action

        feat = 0 * dynamics.get_feat(start)
        action = policy(feat).mode()
        succ, feats, actions = tools.static_scan(
            step, [torch.arange(horizon)], (start, feat, action)
        )
        states = {k: torch.cat([start[k][None], v[:-1]], 0) for k, v in succ.items()}
        if repeats:
            raise NotImplemented("repeats is not implemented in this version")

        return feats, states, actions

    def _compute_target(
        self, imag_feat, imag_state, imag_action, reward, actor_ent, state_ent, slow
    ):
        if "discount" in self._world_model.heads:
            inp = self._world_model.dynamics.get_feat(imag_state)
            discount = self._world_model.heads["discount"](inp).mean
        else:
            discount = self._config.discount * torch.ones_like(reward)
        if self._config.future_entropy and self._config.actor_entropy() > 0:
            reward += self._config.actor_entropy() * actor_ent
        if self._config.future_entropy and self._config.actor_state_entropy() > 0:
            reward += self._config.actor_state_entropy() * state_ent
        if slow:
            value = self._slow_value(imag_feat).mode()
        else:
            value = self.value(imag_feat).mode()
        if self._config.critic_trans == "symlog":
            # After adding this line there is issue
            value = symexp(value)
        target = tools.lambda_return(
            reward[:-1],
            value[:-1],
            discount[:-1],
            bootstrap=value[-1],
            lambda_=self._config.discount_lambda,
            axis=0,
        )
        weights = torch.cumprod(
            torch.cat([torch.ones_like(discount[:1]), discount[:-1]], 0), 0
        ).detach()
        return target, weights

    def _compute_actor_loss(
        self, imag_feat, imag_state, imag_action, target, actor_ent, state_ent, weights
    ):
        metrics = {}
        inp = imag_feat.detach() if self._stop_grad_actor else imag_feat
        policy = self.actor(inp)
        actor_ent = policy.entropy()
        # Q-val for actor is not transformed using symlog
        target = torch.stack(target, dim=1)
        if self._config.reward_EMA:
            target = self.reward_ema(target)
            metrics["EMA_scale"] = to_np(self.reward_ema.scale)

        if self._config.imag_gradient == "dynamics":
            actor_target = target
        elif self._config.imag_gradient == "reinforce":
            actor_target = (
                policy.log_prob(imag_action)[:-1][:, :, None]
                * (target - self.value(imag_feat[:-1]).mode()).detach()
            )
        elif self._config.imag_gradient == "both":
            actor_target = (
                policy.log_prob(imag_action)[:-1][:, :, None]
                * (target - self.value(imag_feat[:-1]).mode()).detach()
            )
            mix = self._config.imag_gradient_mix()
            actor_target = mix * target + (1 - mix) * actor_target
            metrics["imag_gradient_mix"] = mix
        else:
            raise NotImplementedError(self._config.imag_gradient)
        if not self._config.future_entropy and (self._config.actor_entropy() > 0):
            actor_entropy = self._config.actor_entropy() * actor_ent[:-1][:, :, None]
            actor_target += actor_entropy
            metrics["actor_entropy"] = to_np(torch.mean(actor_entropy))
        if not self._config.future_entropy and (self._config.actor_state_entropy() > 0):
            state_entropy = self._config.actor_state_entropy() * state_ent[:-1]
            actor_target += state_entropy
            metrics["actor_state_entropy"] = to_np(torch.mean(state_entropy))
        actor_loss = -torch.mean(weights[:-1] * actor_target)
        return actor_loss, metrics

    def _update_slow_target(self):
        if self._config.slow_value_target or self._config.slow_actor_target:
            if self._updates % self._config.slow_target_update == 0:
                mix = self._config.slow_target_fraction
                for s, d in zip(self.value.parameters(), self._slow_value.parameters()):
                    d.data = mix * s.data + (1 - mix) * d.data
            self._updates += 1
