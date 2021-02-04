#%%
import os, sys
import torch
import torch.nn.functional as F
import torch.nn as nn
torch.backends.cudnn.deterministic = True # for bincount

from torchsummary import summary

current_path = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.dirname(current_path)
MODEL_DIR = os.path.join(HOME,  'models')
DATA_DIR = os.path.join(HOME,  'data')
sys.path.append(HOME) 
from utils import *
from mlp import *
# %%

'''
Training script using a utility regularizer
'''

DEBUG = False
BATCH_SIZE = 4096
FINETUNE_BATCH_SIZE = 400_000
EPOCHS = 50
FINETUNE_EPOCHS = 20
LEARNING_RATE = 1e-3
WEIGHT_DECAY = 1e-5
EARLYSTOP_NUM = 10
NFOLDS = 1
SCALING = 10
THRESHOLD = 0.5
SEED = 802
get_seed(SEED)

# f = np.median
# f = np.mean
f = median_avg
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

#%%
with timer("Loading train parquet"):
    train_parquet = os.path.join(DATA_DIR, 'train.parquet')
    train = pd.read_parquet(train_parquet)
train = train.loc[train.date > 85].reset_index(drop=True)
#%%
# vanilla actions based on resp
train['action_0'] = (train['resp'] > 0).astype('int')
for c in range(1,5):
    train['action_'+str(c)] = (train['resp_'+str(c)] > 0).astype('int')
    print(f'action based on resp_{c} mean: ' ,' '*10, train['action_'+str(c)].astype(int).mean())

resp_cols = ['resp', 'resp_1', 'resp_2', 'resp_3', 'resp_4']
target_cols = ['action_0', 'action_1', 'action_2', 'action_3', 'action_4']

feat_cols = [f'feature_{i}' for i in range(130)]
# %%
# feat_cols = [c for c in train.columns if 'feature' in c]
f_mean = np.mean(train[feat_cols[1:]].values, axis=0)
train.fillna(train.mean(),inplace=True)

valid = train.loc[train.date >= 450].reset_index(drop=True)
train = train.loc[train.date < 450].reset_index(drop=True)
# %%
train['cross_41_42_43'] = train['feature_41'] + train['feature_42'] + train['feature_43']
train['cross_1_2'] = train['feature_1'] / (train['feature_2'] + 1e-5)
valid['cross_41_42_43'] = valid['feature_41'] + valid['feature_42'] + valid['feature_43']
valid['cross_1_2'] = valid['feature_1'] / (valid['feature_2'] + 1e-5)

feat_cols.extend(['cross_41_42_43', 'cross_1_2'])
# %%
train_set = ExtendedMarketDataset(train, features=feat_cols, targets=target_cols, resp=resp_cols)
train_loader = DataLoader(train_set, batch_size=BATCH_SIZE, shuffle=True, num_workers=8)

valid_set = ExtendedMarketDataset(valid, features=feat_cols, targets=target_cols, resp=resp_cols)
valid_loader = DataLoader(valid_set, batch_size=BATCH_SIZE, shuffle=False, num_workers=8)

# sanity check
# item = next(iter(train_loader))
# print(item)
# %%
model = ResidualMLP(output_size=len(target_cols))
model.to(device)
summary(model, input_size=(len(feat_cols), ))

optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)
# optimizer = Lookahead(optimizer=optimizer, k=10, alpha=0.5)
scheduler = None
# scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer=optimizer, pct_start=0.1, div_factor=1e3,
#                                                 max_lr=1e-2, epochs=EPOCHS, 
#                                                 steps_per_epoch=len(train_loader))
loss_fn = SmoothBCEwLogits(smoothing=0.005)

es = EarlyStopping(patience=EARLYSTOP_NUM, mode="max")

# %%
if DEBUG:
    regularizer = UtilityLoss(alpha=1e-4, scaling=10)
    data = next(iter(train_loader))
    optimizer.zero_grad()
    features = data['features'].to(device)
    label = data['label'].to(device)
    weight = data['weight'].view(-1).to(device)
    resp = data['resp'].to(device)
    date = data['date'].view(-1).to(device)
    model.eval()
    outputs = model(features)
    loss = loss_fn(outputs, label)
    # loss += regularizer(outputs, resp, weight=weight, date=date)
    # loss.backward()
# %%
get_seed(1127802)
_fold = 4
model_weights = os.path.join(MODEL_DIR, f"resmlp_{_fold}.pth")
try:
    model.load_state_dict(torch.load(model_weights))
except:
    model.load_state_dict(torch.load(model_weights, map_location=torch.device('cpu')))
model.eval()
valid_pred = valid_epoch(model, valid_loader, device)
valid_auc, valid_score = get_valid_score(valid_pred, valid, 
                                        f=median_avg, threshold=0.5, target_cols=target_cols)

print(f"val_utility:{valid_score:.2f}  valid_auc:{valid_auc:.4f}")
# %%
'''
fine-tuning the trained model utility score
current best setting: bathsize = 100k
to-do: using the least square loss to model w_{ij} res[ij]
'''
FINETUNE_BATCH_SIZE = 102_400
regularizer = UtilityLoss(alpha=1, scaling=12)
finetune_loader = DataLoader(train_set, batch_size=FINETUNE_BATCH_SIZE, shuffle=True, num_workers=8)
finetune_optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE*1e-2)

FINETUNE_EPOCHS = 1
for epoch in range(FINETUNE_EPOCHS):
    tqdm.write(f"\nFine tuning epoch {epoch+1}")
    _ = train_epoch_utility(model, finetune_optimizer, scheduler, 
                            regularizer, finetune_loader, device)
    # train_loss = train_epoch(model, finetune_optimizer, scheduler, 
    #                          loss_fn, finetune_loader, device)                            
    valid_pred = valid_epoch(model, valid_loader, device)
    valid_auc, valid_score = get_valid_score(valid_pred, valid, 
                                            f=median_avg, threshold=0.5, target_cols=target_cols)

    tqdm.write(f"\nval_utility:{valid_score:.2f}  valid_auc:{valid_auc:.4f}")
# %%
torch.save(model.state_dict(), MODEL_DIR+f"/resmlp_finetune_fold_{_fold}.pth")
# %%
#%%
# regularizer = UtilityLoss(alpha=1e-4, scaling=12)

# finetune_loader = DataLoader(train_set, batch_size=FINETUNE_BATCH_SIZE, shuffle=True, num_workers=8)
# finetune_optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE*1e-3)


# for epoch in range(EPOCHS):

#     start_time = time()
#     train_loss = train_epoch(model, optimizer, scheduler, loss_fn, train_loader, device)
        
#     train_loss = train_epoch_utility(model, finetune_optimizer, scheduler, 
#                                          loss_fn, regularizer, finetune_loader, device)

#     valid_pred = valid_epoch(model, valid_loader, device)
#     valid_auc, valid_score = get_valid_score(valid_pred, valid, 
#                                         f=median_avg, threshold=0.5, target_cols=target_cols)
#     model_file = MODEL_DIR+f"/resmlp_seed_{SEED}_util_{int(valid_score)}_auc_{valid_auc:.4f}.pth"
#     es(valid_auc, model, model_path=model_file, epoch_utility_score=valid_score)

#     print(f"\nEPOCH:{epoch:2d} tr_loss:{train_loss:.2f}  "
#                 f"val_utility:{valid_score:.2f} valid_auc:{valid_auc:.4f}  "
#                 f"epoch time: {time() - start_time:.1f}sec  "
#                 f"early stop counter: {es.counter}\n")
    
#     if es.early_stop:
#         print("\nEarly stopping")
#         break
