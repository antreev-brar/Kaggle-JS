#%%
import os
import sys
import pandas as pd
import numpy as np
from tqdm import tqdm
from sklearn.metrics import roc_auc_score
import torch
import tensorflow as tf
from numba import njit
import random
import datetime

HOME = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = HOME+'/models/'
DATA_DIR = HOME+'/data/'
from utils import *
from utils_js import *
# from nn.mlp import *

DEBUG = False
SEED = 1111
START_SIMU_TEST = 490 # this day to 499 as simulated test days
END_SIMU_TEST = 499
TQDM_INT = 20
batch_size = 5000
label_smoothing = 1e-2
learning_rate = 1e-3

GPU = False

if GPU:
    gpus = tf.config.experimental.list_physical_devices(device_type="GPU")
    tf.config.experimental.set_visible_devices(devices=gpus[0], device_type="GPU")
    tf.config.experimental.set_memory_growth(device=gpus[0], enable=True)
else:
    cpus = tf.config.experimental.list_physical_devices(device_type='CPU')
    tf.config.experimental.set_visible_devices(devices= cpus, device_type='CPU')


#%%
'''
The mock test set is taken after the Purged Time series CV split last fold's test set:
i.e., START_SIMU_TEST date needs to be > 382

Reference:
https://www.kaggle.com/jorijnsmit/found-the-holy-grail-grouptimeseriessplit
https://www.kaggle.com/tomwarrens/purgedgrouptimeseriessplit-stacking-ensemble-mode
'''

with timer("Loading train parquet"):
    train_parquet = os.path.join(DATA_DIR, 'train.parquet')
    train = pd.read_parquet(train_parquet)
# print(train.info())

train['action'] = (train['resp'] > 0)
for c in range(1,5):
    train['action'] = train['action'] & ((train['resp_'+str(c)] > 0))
features = [c for c in train.columns if 'feature' in c]

resp_cols = ['resp', 'resp_1', 'resp_2', 'resp_3', 'resp_4']

# X = train[features].values
# y = np.stack([(train[c] > 0).astype('int') for c in resp_cols]).T #Multitarget

f_mean = np.mean(train[features[1:]].values, axis=0)

simu_test = train.query(f'date > {START_SIMU_TEST} & date <= {END_SIMU_TEST}').reset_index(drop = True) 
print(f"Simulated public test file length: {len(simu_test)}")

#%%
class Iter_Valid(object):

    global predicted
    predicted = []

    def __init__(self, df, features, batch_size = 1):
        df = df.reset_index(drop=True)
        self.columns = ['weight'] + features + ['date']
        self.df = df[self.columns]
        self.weight = df['weight'].astype(float).values
        self.action = df['action'].astype(int).values
        self.pred_df = df[['action']]
        # self.pred_df[['action']] = 0
        self.len = len(df)
        self.current = 0
        self.batch_size = batch_size

    def __iter__(self):
        return self

    def __next__(self):
        pre_start = self.current
        self.current += self.batch_size
        if self.current <= self.len:
            df = self.df[pre_start:self.current].copy()
            pred_df = self.pred_df[pre_start:self.current].copy()
            return df, pred_df
        elif self.current > self.len and (self.current - self.len < self.batch_size):
            df = self.df[pre_start:self.len].copy()
            pred_df = self.pred_df[pre_start::self.len].copy()
            return df, pred_df
        else:
            raise StopIteration()

    def predict(self,pred_df):
        predicted.append(pred_df)
# %% seed 1111 overfit model
hidden_units = [150, 150, 150]
dropout_rates = [0.2, 0.2, 0.2, 0.2]

def create_mlp_tf(
    num_columns, num_labels, hidden_units, dropout_rates, label_smoothing, learning_rate
):

    inp = tf.keras.layers.Input(shape=(num_columns,))
    x = tf.keras.layers.BatchNormalization()(inp)
    x = tf.keras.layers.Dropout(dropout_rates[0])(x)
    for i in range(len(hidden_units)):
        x = tf.keras.layers.Dense(hidden_units[i])(x)
        x = tf.keras.layers.BatchNormalization()(x)
        x = tf.keras.layers.Activation(tf.keras.activations.swish)(x)
        x = tf.keras.layers.Dropout(dropout_rates[i + 1])(x)

    x = tf.keras.layers.Dense(num_labels)(x)
    out = tf.keras.layers.Activation("sigmoid")(x)

    model = tf.keras.models.Model(inputs=inp, outputs=out)
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=learning_rate),
        loss=tf.keras.losses.BinaryCrossentropy(label_smoothing=label_smoothing),
        metrics=tf.keras.metrics.AUC(name="AUC"),
    )
    return model


model = create_mlp_tf(num_columns=len(features), 
                      num_labels=5, 
                      hidden_units=hidden_units, 
                      dropout_rates=dropout_rates, 
                      label_smoothing=label_smoothing, 
                      learning_rate=learning_rate)

model.load_weights(os.path.join(MODEL_DIR,f'model_{SEED}.hdf5'))
model.summary()
models = []
models.append(model)

#%% 10k pytorch model

#%%
if DEBUG:
    '''
    Old testing code here: using class is much faster than iterrows() of pandas
    '''
    test_columns = ['weight'] + features + ['date']
    predicted = []
    def set_predict(df):
        predicted.append(df)

    test_len = 1_000
    start = time()
    with tqdm(total=test_len) as pbar:
        for idx, row in simu_test.iterrows():
            row = pd.DataFrame(row.values.reshape(1,-1), columns=list(row.index))
            test_df = row[test_columns].astype(float)
            pred_df = row[['action']].astype(int)
            pred_df.action = (random.random() > 0.7)
            set_predict(pred_df)
            
            time_taken = time() - start
            total_time_est = time_taken / (idx+1) * 1000000 / 60
            pbar.set_description(f"Current speed = {total_time_est:.2f} minutes to complete inference")
            pbar.update(1)

            if idx >= test_len:
                break


# %%

if __name__ == '__main__':
    '''
    inference simulation
    Using a customized class


    For the seed = 1111 overfit model for day 490-499:
    np.mean: 815.71 
    np.median: 893.32
    avg median: 838.97
    thresh 0.51 + np.median: 824.71
    thresh 0.501 + np.median: 878.82
    thresh 0.498 + np.median: 902.64
    thresh 0.499 + np.median: 893.70
    thresh 0.4985 + np.median: 908.28


    '''
    date = simu_test['date'].values
    weight = simu_test['weight'].values
    resp = simu_test['resp'].values
    action = simu_test['action'].values

    # f = np.mean # 
    f = np.median 
    # f = median_avg 

    THRESHOLD = 0.4985

    iter_test = Iter_Valid(simu_test, features, batch_size=1)
    start = time()

    pbar = tqdm(total=len(simu_test))
    for idx, (test_df, pred_df) in enumerate(iter_test):

        if test_df['weight'].item() > 0:
            x_tt = test_df.loc[:, features].values
            if np.isnan(x_tt[:, 1:].sum()):
                x_tt[:, 1:] = np.nan_to_num(x_tt[:, 1:]) + np.isnan(x_tt[:, 1:]) * f_mean
            pred = np.mean([model(x_tt, training = False).numpy() for model in models],axis=0)
            pred = f(pred.squeeze())
            pred_df.action = np.where(pred >= THRESHOLD, 1, 0).astype(int)
        else:
            pred_df.action = 0

        iter_test.predict(pred_df)

        time_taken = time() - start
        total_time_est = time_taken / (idx+1) * 1000000 / 60
        pbar.set_description(f"Current speed = {total_time_est:.2f} minutes to complete inference")
        pbar.update()

    y_true = simu_test['action']
    y_pred = pd.concat(predicted)['action']
    print('\nValidation auc:', roc_auc_score(y_true, y_pred))
    score = utility_score_bincount(date, weight, resp, y_true)
    score_pred = utility_score_bincount(date, weight, resp, y_pred)
    print('\nMax possible utility score:', score)
    print('\nModel utility score:       ', score_pred)