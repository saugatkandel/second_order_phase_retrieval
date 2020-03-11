#Author - Saugat Kandel
# coding: utf-8


import numpy as np
import tensorflow as tf
from tensorflow.python.ops.gradients_impl import _hessian_vector_product
from typing import Callable, NamedTuple
from sopt.optimizers.tensorflow.utils import MatrixFreeLinearOp, conjugate_gradient, BackTrackingLineSearch


class LMState(NamedTuple):
    mu_old: tf.Tensor
    mu_new: tf.Tensor
    dx: tf.Tensor
    loss: tf.Tensor
    actual_reduction: tf.Tensor
    pred_reduction: tf.Tensor
    ratio: tf.Tensor
    cgi: tf.Tensor
    converged: tf.Tensor
    i: tf.Tensor
    v_const: tf.Tensor

class LMA(object):
    def __init__(self,
                 input_var: tf.Variable,
                 predictions_fn: Callable[[tf.Tensor], tf.Tensor],
                 loss_fn: Callable[[tf.Tensor], tf.Tensor],
                 name: str,
                 mu: float = 1e-4,
                 grad_norm_regularization_power: float = 1.0,
                 mu_contraction: float = 2 / 3,
                 update_cond_thres_low: float = 0.25,
                 update_cond_thres_high: float = 0.75,
                 mu_thres_low: float = 1e-8,
                 mu_thres_high: float = 1e8,
                 max_mu_linesearch_iters: int = None,
                 max_cg_iter: int = 50,
                 min_cg_tol: float = 1e-1,  # Force CG iterations to have at least this tolerance.s
                 ftol: float = 1e-6,
                 gtol: float = 1e-6,
                 min_reduction_ratio: float = 1e-4,
                 hessian_fn: Callable[[tf.Tensor], tf.Tensor]= None,
                 assert_tolerances: bool = True) -> None:

        self._name = name
        self._input_var = input_var
        self._machine_eps = np.finfo(input_var.dtype.as_numpy_dtype).eps

        self._predictions_fn = predictions_fn
        self._loss_fn = loss_fn

        self._preds_t = self._predictions_fn(self._input_var)
        self._loss_t = self._loss_fn(self._preds_t)

        # Multiplicating factor to update the damping factor at the end of each cycle
        self._mu_contraction = mu_contraction
        self._update_cond_thres_low = update_cond_thres_low
        self._update_cond_thres_high =  update_cond_thres_high
        self._mu_thres_low = mu_thres_low
        self._mu_thres_high = mu_thres_high
        self._grad_norm_regularization_power = grad_norm_regularization_power

        self._max_linesearch_iters = max_mu_linesearch_iters
        if max_mu_linesearch_iters is None:
            range = self._mu_thres_high / self._mu_thres_low
            self._max_linesearch_iters = np.ceil(-np.log(range) / np.log(mu_contraction)).astype('int32')

        self._max_cg_iter = max_cg_iter
        self._min_cg_tol = min_cg_tol

        self._min_reduction_ratio = min_reduction_ratio
        self._ftol, self._gtol = self._check_tolerance(ftol, gtol)
        self._assert_tolerances = assert_tolerances

        self._hessian_fn = hessian_fn

        self._projected_gradient_linesearch = BackTrackingLineSearch()

        with tf.variable_scope(name):
            self._mu = tf.get_variable("lambda", dtype=tf.float32,
                                                   initializer=mu,
                                                   trainable=False)
            self._update_var = tf.get_variable("delta", dtype=tf.float32,
                                               initializer=tf.zeros_like(self._input_var),
                                               trainable=False)
            self._dummy_var = tf.get_variable("dummy", dtype=tf.float32,
                                              initializer=tf.zeros_like(self._preds_t),
                                              trainable=False)

            self._loss_before_update = tf.get_variable("loss_before_update", dtype=tf.float32,
                                                     initializer=0.,
                                                     trainable=False)

            self._iteration = tf.get_variable("iteration", shape=[], dtype=tf.int32,
                                              initializer=tf.zeros_initializer,
                                              trainable=False)

            self._total_cg_iterations = tf.get_variable("total_cg_iterations",
                                                        dtype=tf.int32, shape=[],
                                                        initializer=tf.zeros_initializer,
                                                        trainable=False)
            self._total_proj_ls_iterations = tf.get_variable("total_projection_line_search_iterations",
                                                             dtype=tf.int32, shape=[],
                                                             initializer=tf.zeros_initializer,
                                                             trainable=False)
        # Set up the second order calculations to define matrix-free linear ops.
        self._setup_second_order()

    def _check_tolerance(self, ftol, gtol):
        """This is adapted almost exactly from the corresponding scipy function"""
        def check(tol, name):
            if (tol is None) or (tol < self._machine_eps):
                tol = self._machine_eps
                print(f"If {name} tolerance is set to None or to below the machine epsilon ({self._machine_eps:.2e}), "
                      + "it is reset to the machine epsilon by default.")
            return tol

        ftol = check(ftol, "ftol")
        gtol = check(gtol, "gtol")
        return ftol, gtol

    def _setup_hessian_vector_product(self, 
                                      jvp_fn: Callable[[tf.Tensor], tf.Tensor],
                                      x: tf.Tensor,
                                      v_constant: tf.Tensor) -> tf.Tensor:
        predictions_this = self._predictions_fn(v_constant)
        loss_this = self._loss_fn(predictions_this)
        
        if self._hessian_fn is None:
            hjvp = _hessian_vector_product(ys=[loss_this],
                                           xs=[predictions_this],
                                           v=[jvp_fn(x)])
        else:
            hjvp = self._hessian_fn(predictions_this) * jvp_fn(x)
        
        jhjvp = tf.gradients(predictions_this, v_constant, hjvp)[0]
        return jhjvp
        
    def _setup_second_order(self) -> None:
        with tf.name_scope(self._name + '_gngvp'):
            self.vjp_fn = lambda x: tf.gradients(self._preds_t, self._input_var, x,
                                                 stop_gradients=[x], name="vjp")[0]
            jvp_fn = lambda x: tf.gradients(self.vjp_fn(self._dummy_var), self._dummy_var, x, name='jvpz')[0]
            self.jvp_fn = jvp_fn
            
            self._jhjvp_fn = lambda x, v_constant: self._setup_hessian_vector_product(jvp_fn, x, v_constant)
            self._loss_grads = tf.gradients(self._loss_t, self._preds_t)[0]
            self._grads = self.vjp_fn(self._loss_grads)

    @staticmethod
    def _applyConstraint(input_var, lma_update):
        return input_var.constraint(input_var + lma_update)


    def _applyProjectedGradient(self, lmstate):#, lm_update, beta = 0.5, sigma=1e-5):
        """
        Reference:
        Journal of Computational and Applied Mathematics 172 (2004) 375–397
        """

        projected_var = self._applyConstraint(self._input_var, lmstate.dx)
        projected_loss_new = self._loss_fn(self._predictions_fn(projected_var))
        projected_loss_change = self._loss_before_update - projected_loss_new
        projection_reduction_ratio = projected_loss_change / tf.abs(self._loss_before_update)

        fconv = tf.abs(self._loss_before_update) <= self._machine_eps

        no_projection_condition = ((projection_reduction_ratio > self._min_reduction_ratio) | fconv)


        print_op = tf.print('loss_before_LM', self._loss_t,
        #                    'loss_after_lm', loss_new,
        #                    'reduction_ration', reduction_ratio,
                            'loss_after_lm', lmstate.loss,
                            'loss_after_constraint', projected_loss_new,
                            'projected_loss_diff', projected_loss_change,
                            'projection_reduction_ratio', projection_reduction_ratio,
                            'projection_condition', no_projection_condition)
        #                    'test_rhs', test_rhs, 'dist', dist)


        def _loss_and_update_fn(x, y):
            update = self._applyConstraint(x, y)
            loss = self._loss_fn(self._predictions_fn(update))
            return loss, update

        def _linesearch():
            linesearch_state = self._projected_gradient_linesearch.search(objective_and_update=_loss_and_update_fn,
                                                                          x0=self._input_var,
                                                                          descent_dir=-self._grads,
                                                                          gradient=self._grads,
                                                                          f0=self._loss_before_update)
            counter_op = self._total_proj_ls_iterations.assign_add(linesearch_state.step_count)
            with tf.control_dependencies([counter_op]):
                output = tf.identity(linesearch_state.newx)
            return output

        #with tf.control_dependencies([print_op]):
        input_var_update = tf.cond(no_projection_condition,
                                       lambda: projected_var,
                                       _linesearch)
        return input_var_update


    def _projected_gradient_linesearch_old(self, beta = 0.5, sigma=1e-5):

        iter_max = np.ceil(-np.log(self._machine_eps) * np.log(beta))

        class LSState(NamedTuple):
            i: tf.Tensor
            x: tf.Tensor

        def ls_step(ls_state):
            unconstrained_update = self._input_var - beta ** tf.cast((ls_state.i + 1), tf.float32) * self._grads
            x = self._input_var.constraint(unconstrained_update)
            return LSState(i=ls_state.i + 1, x=x)

        def stopping_criterion(ls_state):
            loss_new = self._loss_fn(self._predictions_fn(ls_state.x))
            loss_diff = loss_new - self._loss_t
            rhs = sigma * tf.tensordot(self._grads, ls_state.x - self._input_var, 1)
            with tf.control_dependencies([tf.print('linesearch iter', ls_state.i,
                                                   #'grads', self._grads,
                                                   #'rhs_term2', ls_state.x - self._input_var,
                                                   #'dot_prod',  tf.tensordot(self._grads, ls_state.x - self._input_var, 1),
                                                   'loss_old', self._loss_t,
                                                   'loss new', loss_new,
                                                   'loss_change', loss_diff,
                                                   #'rhs', rhs,
                                                   'condition', loss_diff > rhs)]):
                rhs = tf.identity(rhs)
            return loss_diff > rhs

        with tf.name_scope('proj_ls_while_loop'):
            ls_state0 = LSState(i=0, x=tf.zeros_like(self._input_var))
            ls_state = tf.while_loop(cond=stopping_criterion,
                                     body=ls_step,
                                     loop_vars=[ls_state0],
                                     maximum_iterations=50,
                                     back_prop=False)
        ls_counter_op = self._total_proj_ls_iterations.assign_add(ls_state.i)
        with tf.control_dependencies([ls_counter_op]):
            output = tf.identity(ls_state.x)
        return output


    def minimize(self) -> tf.Operation:
        tf.logging.warning("The ftol, gtol, and xtol conditions are adapted from "
                           + "https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.least_squares.html."
                           + "This is a test version, and there is no guarantee that these work as intended.")
        with tf.name_scope(self._name + '_minimize_step'):

            store_loss_op = tf.assign(self._loss_before_update, self._loss_t,
                                      name='store_loss_op')
            
            jhjvp_fn_l_h = lambda l, h, v_constant: self._jhjvp_fn(h, v_constant) + l * h
            linear_b = -self._grads

            grad_norm = tf.linalg.norm(self._grads)
            this_forcing_eta = tf.reduce_min([0.5, grad_norm ** 0.5, self._min_cg_tol])

            grad_norm_regularization = tf.linalg.norm(self._loss_grads) ** self._grad_norm_regularization_power

            def _damping_linesearch_step(state: LMState):
                damping = state.mu_new * grad_norm_regularization
                linear_ax = MatrixFreeLinearOp(lambda h: jhjvp_fn_l_h(damping, h, state.v_const),
                                               tf.TensorShape((self._input_var.shape.dims[0],
                                                               self._input_var.shape.dims[0])))
                cg_solve = conjugate_gradient(operator=linear_ax,
                                              rhs=linear_b,
                                              x=None,
                                              tol=this_forcing_eta,
                                              max_iter=self._max_cg_iter)
                cg_update = tf.identity(cg_solve.x, name='cg_solved')
                optimized_var = self._input_var + cg_update
                pred_reduction = -0.5 * tf.tensordot(cg_update, damping * cg_update + linear_b, 1)

                loss_new = self._loss_fn(self._predictions_fn(optimized_var))
                actual_reduction = loss_new - self._loss_before_update
                ratio = tf.cond(tf.math.not_equal(pred_reduction, 0.),
                                lambda: actual_reduction / pred_reduction,
                                lambda: 0.)
                f1 = lambda: tf.constant(1.0 / self._mu_contraction)
                f2 = lambda: tf.constant(self._mu_contraction)
                f3 = lambda: tf.constant(1.0)

                update_factor = tf.case({tf.less(ratio, self._update_cond_thres_low):f1,
                                         tf.greater(ratio, self._update_cond_thres_high):f2},
                                        default=f3, exclusive=True)

                mu_new = tf.clip_by_value(state.mu_new * update_factor,
                                          self._mu_thres_low,
                                          self._mu_thres_high)

                state_new = state._replace(mu_old=state.mu_new,
                                           mu_new=mu_new,
                                           loss=loss_new,
                                           dx=cg_update,
                                           ratio=ratio,
                                           actual_reduction=actual_reduction,
                                           pred_reduction=pred_reduction,
                                           cgi=state.cgi + cg_solve.i,
                                           i=state.i + 1)
                converged = self._checkConvergenceAndTolerances(state_new)
                state_new = state_new._replace(converged=converged)
                return [state_new]

            def _damping_linesearch_cond(state: LMState):
                #print_op = tf.print(state.i, state.ratio, state.converged, state.pred_reduction, state.actual_reduction)
                #with tf.control_dependencies([print_op]):
                stop_cond = ~((state.converged > 0)
                              | (state.ratio >= self._min_reduction_ratio))
                return stop_cond

            # Check for gradient convergence and tolerance
            grad_inf_norm = tf.norm(self._grads, ord=np.inf)
            grad_converged = tf.cond(grad_inf_norm < self._gtol, lambda: 1, lambda: 0)

            lmstate0 = LMState(mu_old=self._mu,
                               mu_new=self._mu,
                               dx=tf.zeros_like(self._update_var),
                               loss=tf.constant(0., dtype='float32'),
                               actual_reduction=tf.constant(0., dtype='float32'),
                               pred_reduction=tf.constant(1., dtype='float32'),
                               ratio=tf.constant(0., dtype='float32'),
                               cgi=tf.constant(0, dtype='int32'),
                               v_const=self._input_var,
                               converged=grad_converged,
                               i=tf.constant(0, dtype='int32'))

            while_loop_op = lambda: tf.while_loop(_damping_linesearch_cond,
                                                  _damping_linesearch_step,
                                                  [lmstate0],
                                                  back_prop=False)
            with tf.control_dependencies([store_loss_op]):
                lmstate = tf.cond(grad_converged > 0, lambda: [lmstate0], while_loop_op)

            # Update x only if the reduction ratio changed sufficiently
            dx_new = tf.cond(lmstate.ratio >= self._min_reduction_ratio,
                             lambda: lmstate.dx,
                             lambda: tf.zeros_like(self._update_var))

            assert_ops = []
            if self._assert_tolerances:
                message_str = "Meets one of the convergence criterions or strict tolerance criterions. " +\
                              "Check returned code for detailed information."
                assert_op = tf.assert_greater(lmstate.converged, 0, summarize=1,
                                              message=message_str)
                assert_ops.append(assert_op)
            with tf.control_dependencies(assert_ops):
                if self._input_var.constraint is not None:
                    updated_var = tf.cond(grad_converged > 0,
                                          lambda: self._input_var,
                                          lambda: self._applyProjectedGradient(lmstate))
                else:
                    updated_var = self._input_var + dx_new
            update_ops = [tf.assign(self._mu, lmstate.mu_new),
                          tf.assign(self._update_var, dx_new),
                          tf.assign(self._input_var, updated_var)]
            with tf.control_dependencies([*update_ops]):
                counter_ops = [self._total_cg_iterations.assign_add(lmstate.cgi, name='cg_counter_op'),
                                self._iteration.assign_add(lmstate.i, name='counter_op')]
            with tf.control_dependencies(counter_ops):
                output_op = tf.identity(lmstate.converged)

        return output_op

    def _checkConvergenceAndTolerances(self, lmstate):

        fconv = ((tf.abs(lmstate.actual_reduction) <= self._ftol)
                 & (tf.abs(lmstate.pred_reduction) <= self._ftol)
                 & (lmstate.ratio <= 1)
                 & (lmstate.i > 0))
        dtol = (((lmstate.mu_old == self._mu_thres_high)
                 | (lmstate.mu_old == self._mu_thres_low))
                & (lmstate.i > 0))
        itol = lmstate.i >= self._max_linesearch_iters

        convergence_condition = tf.case({fconv: lambda: 2,
                                         dtol: lambda: 3,
                                         itol: lambda: 4},
                                        default=lambda: 0,
                                        exclusive=False)
        return convergence_condition