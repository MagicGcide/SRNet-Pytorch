from datagen import srnet_datagen, get_input_data
from loss import build_discriminator_loss, build_generator_loss
from model import Generator, DiscriminatorMixed, get_vgg_model
from utils import *

device = torch.device(cfg.gpu)


class Trainer:
    def __init__(self):
        self.data_iter = srnet_datagen()

        self.beta1 = cfg.beta1
        self.beta2 = cfg.beta2

        self.vgg_selected_net = get_vgg_model().to(device)

        self.G = Generator().to(device)
        self.D = DiscriminatorMixed().to(device)

        # self.multi_GPU()

        self.g_optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, self.G.parameters()),
                                            cfg.learning_rate, (self.beta1, self.beta2))
        self.d_optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, self.D.parameters()),
                                            cfg.learning_rate, (self.beta1, self.beta2))
        self.g_scheduler = torch.optim.lr_scheduler.ExponentialLR(self.g_optimizer,
                                                                  (cfg.decay_rate ** (1 / cfg.decay_steps)))
        self.d_scheduler = torch.optim.lr_scheduler.ExponentialLR(self.d_optimizer,
                                                                  (cfg.decay_rate ** (1 / cfg.decay_steps)))
        self.g_writer, self.d_writer = None, None

    def multi_GPU(self):
        if torch.cuda.device_count() > 1:
            self.G = torch.nn.DataParallel(self.G, device_ids=[0, 2])
            self.D = torch.nn.DataParallel(self.D, device_ids=[0, 2])

    def train_step(self, data):
        i_t, i_s, t_sk, t_t, t_b, t_f, mask_t = data
        i_t = i_t.to(device)
        i_s = i_s.to(device)
        t_sk = t_sk.to(device)
        t_t = t_t.to(device)
        t_b = t_b.to(device)
        t_f = t_f.to(device)
        mask_t = mask_t.to(device)

        inputs = [i_t, i_s]
        labels = [t_sk, t_t, t_b, t_f]

        o_sk, o_t, o_b, o_f = self.G(inputs)

        i_db_true = torch.cat([t_b, i_s], dim=1)
        i_db_pred = torch.cat([o_b, i_s], dim=1)
        i_db = torch.cat([i_db_true, i_db_pred], dim=0)

        i_df_true = torch.cat([t_f, i_t], dim=1)
        i_df_pred = torch.cat([o_f, i_t], dim=1)
        i_df = torch.cat([i_df_true, i_df_pred], dim=0)

        # d_loss
        # 在前面G的训练已经更新了其参数，这里不再更新G的参数，否则会导致变量inplace操作，使得反向传播出现错误
        o_db, o_df = self.D([i_db.detach(), i_df.detach()])

        db_loss = build_discriminator_loss(o_db)
        df_loss = build_discriminator_loss(o_df)
        d_loss_detail = [db_loss, df_loss]
        d_loss = torch.add(db_loss, df_loss)

        self.reset_grad()
        d_loss.backward()
        self.d_optimizer.step()

        # g_loss
        o_db, o_df = self.D([i_db, i_df])

        i_vgg = torch.cat([t_f, o_f], dim=0)
        vgg_layers = [1, 6, 11, 20, 29]
        out_vgg = []
        X = i_vgg
        for i in range(len(self.vgg_selected_net)):
            X = self.vgg_selected_net[i](X)
            if i in vgg_layers:
                out_vgg.append(X)

        out_g = [o_sk, o_t, o_b, o_f, mask_t]
        out_d = [o_db, o_df]

        g_loss, g_loss_detail = build_generator_loss(out_g, out_d, labels, out_vgg)

        self.reset_grad()
        g_loss.backward()
        self.g_optimizer.step()

        self.g_scheduler.step()
        self.d_scheduler.step()

        return d_loss, g_loss, d_loss_detail, g_loss_detail

    def reset_grad(self):
        self.d_optimizer.zero_grad()
        self.g_optimizer.zero_grad()

    def train(self):
        if not cfg.train_name:
            train_name = get_train_name()
        else:
            train_name = cfg.train_name

        if sys.platform.startswith('win'):
            self.d_writer = SummaryWriter('model_logs\\train_logs\\' + train_name + '\\discriminator')
            self.g_writer = SummaryWriter('model_logs\\train_logs\\' + train_name + '\\generator')
        else:
            self.d_writer = SummaryWriter(os.path.join(cfg.tensorboard_dir, train_name, 'discriminator'))
            self.g_writer = SummaryWriter(os.path.join(cfg.tensorboard_dir, train_name, 'generator'))

        for step in range(cfg.max_iter):
            global_step = step + 1
            # 训练、获取损失
            d_loss, g_loss, d_loss_detail, g_loss_detail = self.train_step(next(self.data_iter))

            # 打印loss信息
            if global_step % cfg.show_loss_interval == 0 or step == 0:
                print_log("step: {:>6d}   d_loss: {:>3.5f}   g_loss: {:>3.5f}".format(global_step, d_loss, g_loss))
                print('d_loss_detail: ' + str([float('%.4f' % d.data) for d in d_loss_detail]))
                print('g_loss_detail: ' + str([float('%.4f' % g.data) for g in g_loss_detail]))

            # 写tensorboard
            if global_step % cfg.write_log_interval == 0:
                self.write_summary(d_loss, d_loss_detail, g_loss, g_loss_detail, global_step)

            # 生成example
            if global_step % cfg.gen_example_interval == 0:
                save_dir = os.path.join(cfg.example_result_dir, train_name,
                                        'iter-' + str(global_step).zfill(len(str(cfg.max_iter))))
                self.predict_data_list(save_dir, get_input_data())
                print_log("example generated in dir {}".format(save_dir), content_color=PrintColor['green'])

            # 保存模型
            if global_step % cfg.save_ckpt_interval == 0:
                save_dir = os.path.join(cfg.checkpoint_save_dir, train_name,
                                        'iter-' + str(global_step).zfill(len(str(cfg.max_iter))))
                self.save_checkpoint(save_dir)
                print_log("checkpoint saved in dir {}".format(save_dir), content_color=PrintColor['green'])
        print_log('training finished.', content_color=PrintColor['yellow'])

    def save_checkpoint(self, save_dir):
        os.makedirs(save_dir)
        torch.save(self.G.state_dict(), os.path.join(save_dir, 'G.pth'))
        torch.save(self.D.state_dict(), os.path.join(save_dir, 'D_f.pth'))

    def predict(self, i_t, i_s, to_shape=None):
        assert i_t.shape == i_s.shape and i_t.dtype == i_s.dtype

        i_t, i_s, to_shape = pre_process_img(i_s, i_t, to_shape)

        o_sk, o_t, o_b, o_f = self.G([i_t.to(device), i_s.to(device)])

        o_sk = o_sk.data.cpu()
        o_t = o_t.data.cpu()
        o_b = o_b.data.cpu()
        o_f = o_f.data.cpu()

        transpose_vector = [0, 2, 3, 1]
        o_sk = o_sk.permute(transpose_vector).numpy()
        o_t = o_t.permute(transpose_vector).numpy()
        o_b = o_b.permute(transpose_vector).numpy()
        o_f = o_f.permute(transpose_vector).numpy()

        o_sk = cv2.resize((o_sk[0] * 255.).astype(np.uint8), to_shape, interpolation=cv2.INTER_NEAREST)
        o_t = cv2.resize(((o_t[0] + 1.) * 127.5).astype(np.uint8), to_shape)
        o_b = cv2.resize(((o_b[0] + 1.) * 127.5).astype(np.uint8), to_shape)
        o_f = cv2.resize(((o_f[0] + 1.) * 127.5).astype(np.uint8), to_shape)

        return [o_sk, o_t, o_b, o_f]

    def predict_data_list(self, save_dir, input_data_list, mode=1):
        for data in input_data_list:
            i_t, i_s, original_shape, data_name = data
            result = self.predict(i_t, i_s, original_shape)
            save_result(save_dir, result, data_name, mode=mode)

    def write_summary(self, d_loss, d_loss_detail, g_loss, g_loss_detail, step):
        self.d_writer.add_scalar('loss', d_loss, step)
        self.d_writer.add_scalar('l_db', d_loss_detail[0], step)
        self.d_writer.add_scalar('l_df', d_loss_detail[1], step)

        self.g_writer.add_scalar('loss', g_loss, step)
        self.g_writer.add_scalar('l_t_sk', g_loss_detail[0], step)
        self.g_writer.add_scalar('l_t_l1', g_loss_detail[1], step)
        self.g_writer.add_scalar('l_b_gan', g_loss_detail[2], step)
        self.g_writer.add_scalar('l_b_l1', g_loss_detail[3], step)
        self.g_writer.add_scalar('l_f_gan', g_loss_detail[4], step)
        self.g_writer.add_scalar('l_f_l1', g_loss_detail[5], step)
        self.g_writer.add_scalar('l_f_vgg_per', g_loss_detail[6], step)
        self.g_writer.add_scalar('l_f_vgg_style', g_loss_detail[7], step)


if __name__ == '__main__':
    Trainer().train()
