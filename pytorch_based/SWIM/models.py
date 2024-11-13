from typing import Tuple, List, Union, Any, Optional, Dict, Literal, Callable, Type
import abc

from tqdm import tqdm
import numpy as np
import torch
import torch.nn as nn
import torch.optim
import torch.utils.data
from torch import Tensor
from sklearn.linear_model import RidgeCV, RidgeClassifierCV


############################################################################
##### Base classes                                                     #####
##### - FittableModule: A nn.Module with .fit(X, y) support            #####
##### - Sequential: chaining together multiple FittableModules         #####
##### - make_fittable: turns type nn.Module into FittableModule        #####
############################################################################


class FittableModule(nn.Module):
    def __init__(self):
        super(FittableModule, self).__init__()
    

    @abc.abstractmethod
    def fit(self, 
            X: Optional[Tensor] = None, 
            y: Optional[Tensor] = None,
        ) -> Tuple[Optional[Tensor], Optional[Tensor]]:
        """Given neurons of the previous layer, and target labels, fit the 
        module. Returns the forwarded activations and labels [f(X), y].

        Args:
            X (Optional[Tensor]): Forward-propagated activations of training data, shape (N, d).
            y (Optional[Tensor]): Training labels, shape (N, p).
        
        Returns:
            Forwarded activations and labels [f(X), y].
        """
        raise NotImplementedError("Method fit must be implemented in subclass.")
        #return self(X), y


class Sequential(FittableModule):
    def __init__(self, *layers: FittableModule):
        """
        Args:
            *layers (FittableModule): Variable length argument list of FittableModules to chain together.
        """
        super(Sequential, self).__init__()
        self.layers = nn.ModuleList(layers)


    def fit(self, X: Tensor, y: Tensor):
        for layer in self.layers:
            X, y = layer.fit(X, y)
        return X, y


    def forward(self, X: Tensor) -> Tensor:
        for layer in self.layers:
            X = layer(X)
        return X
    


def make_fittable(module_class: Type[nn.Module]) -> Type[FittableModule]:
    class FittableModuleWrapper(FittableModule, module_class):
        def __init__(self, *args, **kwargs):
            FittableModule.__init__(self)
            module_class.__init__(self, *args, **kwargs)
        
        def fit(self, 
                X: Optional[Tensor] = None, 
                y: Optional[Tensor] = None,
            ) -> Tuple[Optional[Tensor], Optional[Tensor]]:
            return self(X), y
    
    return FittableModuleWrapper


Tanh = make_fittable(nn.Tanh)
ReLU = make_fittable(nn.ReLU)
Identity = make_fittable(nn.Identity)


############################################################################
##### Layers                                                           #####
##### - Dense: Fully connected layer                                   #####
##### - SWIMLayer                                                      #####
##### - RidgeCV (TODO currently just an sklearn wrapper)               #####
##### - RidgeClassifierCV (TODO currently just an sklearn wrapper)     #####
##### - LogisticRegressionModule                                       #####
############################################################################

class Dense(FittableModule):
    def __init__(self,
                 generator: torch.Generator,
                 in_dim: int,
                 out_dim: int,
                 activation: Optional[nn.Module] = None,
                 ):
        """Dense MLP layer with LeCun weight initialization,
        Gaussan bias initialization."""
        super(Dense, self).__init__()
        self.generator = generator
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.dense = nn.Linear(in_dim, out_dim)
        self.activation = activation
    
    def fit(self, X:Tensor, y:Tensor):
        self.to(X.device)
        with torch.no_grad():
            nn.init.normal_(self.dense.weight, mean=0, std=self.in_dim**-0.5, generator=self.generator)
            nn.init.normal_(self.dense.bias, mean=0, std=self.in_dim**-0.25, generator=self.generator)
            return self(X), y
    
    def forward(self, X):
        X = self.dense(X)
        if self.activation is not None:
            X = self.activation(X)
        return X
    


class SWIMLayer(FittableModule):
    def __init__(self,
                 generator: torch.Generator,
                 in_dim: int, 
                 out_dim: int,
                 activation: Optional[nn.Module] = None,
                 sampling_method: Literal['uniform', 'gradient'] = 'gradient'
                 ):
        """Dense MLP layer with pair sampled weights (uniform or gradient-weighted).

        Args:
            generator (torch.Generator): PRNG object.
            in_dim (int): Input dimension.
            out_dim (int): Output dimension.
            activation (nn.Module): Activation function.
            sampling_method (str): Pair sampling method. Uniform or gradient-weighted.
        """
        super(SWIMLayer, self).__init__()
        self.generator = generator
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.dense = nn.Linear(in_dim, out_dim)
        self.sampling_method = sampling_method
        self.activation = activation


    def fit(self, 
            X: Tensor, 
            y: Tensor,
        ) -> Tuple[Tensor, Tensor]:
        """Given forward-propagated training data X at the previous 
        hidden layer, and supervised target labels y, fit the weights
        iteratively by letting rows of the weight matrix be given by
        pairs of samples from X. See paper for more details.

        Args:
            X (Tensor): Forward-propagated activations of training data, shape (N, d).
            y (Tensor): Training labels, shape (N, p).
        
        Returns:
            Forwarded activations and labels [f(X), y].
        """
        self.to(X.device)
        with torch.no_grad():
            N, d = X.shape
            dtype = X.dtype
            device = X.device
            EPS = torch.tensor(0.1, dtype=dtype, device=device)

            #obtain pair indices
            n = 5*N
            idx1 = torch.arange(0, n, dtype=torch.int32, device=device) % N
            delta = torch.randint(1, N, size=(n,), dtype=torch.int32, device=device, generator=self.generator)
            idx2 = (idx1 + delta) % N
            dx = X[idx2] - X[idx1]
            dists = torch.linalg.norm(dx, axis=1, keepdims=True)
            dists = torch.maximum(dists, EPS)
            
            if self.sampling_method=="gradient":
                #calculate 'gradients'
                dy = y[idx2] - y[idx1]
                y_norm = torch.linalg.norm(dy, axis=1, keepdims=True) #NOTE 2023 paper uses ord=inf instead of ord=2
                grad = (y_norm / dists).reshape(-1) 
                p = grad/grad.sum()
            elif self.sampling_method=="uniform":
                p = torch.ones(n, dtype=dtype, device=device) / n
            else:
                raise ValueError(f"sampling_method must be 'uniform' or 'gradient'. Given: {self.sampling_method}")

            #sample pairs
            selected_idx = torch.multinomial(
                p,
                self.out_dim,
                replacement=True,
                generator=self.generator
                )
            idx1 = idx1[selected_idx]
            dx = dx[selected_idx]
            dists = dists[selected_idx]

            #define weights and biases
            weights = dx / (dists**2)
            biases = -torch.einsum('ij,ij->i', weights, X[idx1]) - 0.5
            self.dense.weight.data = weights
            self.dense.bias.data = biases
            return self(X), y
    

    def forward(self, X):
        X = self.dense(X)
        if self.activation is not None:
            X = self.activation(X)
        return X



##########################################
#### Logistic Regression and RidgeCV  ####
####      classifiers/regressors      ####
##########################################


class RidgeCVModule(FittableModule):
    def __init__(self, alphas=np.logspace(-1, 3, 10)):
        """RidgeCV layer using sklearn's RidgeCV. TODO dont use sklearn"""
        super(RidgeCVModule, self).__init__()
        self.ridge = RidgeCV(alphas=alphas)

    def fit(self, X: Tensor, y: Tensor) -> Tuple[Tensor, Tensor]:
        """Fit the RidgeCV model. TODO dont use sklearn"""
        X_np = X.detach().cpu().numpy().astype(np.float64)
        y_np = y.detach().cpu().squeeze().numpy().astype(np.float64)
        self.ridge.fit(X_np, y_np)
        return self(X), y

    def forward(self, X: Tensor) -> Tensor:
        """Forward pass through the RidgeCV model. TODO dont use sklearn"""
        X_np = X.detach().cpu().numpy().astype(np.float64)
        y_pred_np = self.ridge.predict(X_np)
        return torch.tensor(y_pred_np, dtype=X.dtype, device=X.device).unsqueeze(1) #TODO unsqueeze???



class RidgeClassifierCVModule(FittableModule):
    def __init__(self, alphas=np.logspace(-1, 3, 10)):
        """RidgeClassifierCV layer using sklearn's RidgeClassifierCV. TODO dont use sklearn"""
        super(RidgeClassifierCVModule, self).__init__()
        self.ridge = RidgeClassifierCV(alphas=alphas)

    def fit(self, X: Tensor, y: Tensor) -> Tuple[Tensor, Tensor]:
        """Fit the sklearn ridge model."""
        # Make y categorical from one_hot NOTE assumees y one-hot
        y_cat = torch.argmax(y, dim=1)
        X_np = X.detach().cpu().numpy().astype(np.float64)
        y_np = y_cat.detach().cpu().squeeze().numpy().astype(np.float64)
        self.ridge.fit(X_np, y_np)
        return self(X), y

    def forward(self, X: Tensor) -> Tensor:
        X_np = X.detach().cpu().numpy().astype(np.float64)
        y_pred_np = self.ridge.predict(X_np)
        return torch.tensor(y_pred_np, dtype=X.dtype, device=X.device)



class LogisticRegressionModule(FittableModule):
    def __init__(self, 
                 generator: torch.Generator,
                 batch_size = 512,
                 num_epochs = 30,
                 lr = 0.01,):
        super(LogisticRegressionModule, self).__init__()
        self.generator = generator
        self.model = None
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        self.lr = lr

    def fit(self, X: Tensor, y: Tensor) -> Tuple[Tensor, Tensor]:
        # Determine input and output dimensions
        input_dim = X.size(1)
        if y.dim() > 1 and y.size(1) > 1:
            output_dim = y.size(1)
            y_labels = torch.argmax(y, dim=1)
            criterion = nn.CrossEntropyLoss()
        else:
            output_dim = 1
            y_labels = y.squeeze()
            criterion = nn.BCEWithLogitsLoss()

        # Define the model
        self.model = nn.Linear(input_dim, output_dim)
        device = X.device
        self.model.to(device)

        # Create a CPU generator for DataLoader
        data_loader_generator = torch.Generator(device='cpu')
        data_loader_generator.manual_seed(self.generator.initial_seed())
        dataset = torch.utils.data.TensorDataset(X, y_labels)
        loader = torch.utils.data.DataLoader(
            dataset, 
            batch_size=self.batch_size, 
            shuffle=True,
            generator=data_loader_generator
        )

        # Training loop
        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.lr)
        for epoch in tqdm(range(self.num_epochs)):
            self.model.train()
            for batch_X, batch_y in loader:
                optimizer.zero_grad()
                outputs = self.model(batch_X)
                loss = criterion(outputs, batch_y)
                loss.backward()
                optimizer.step()

        return self(X), y

    def forward(self, X: Tensor) -> Tensor:
        return self.model(X)
    

######################################
#####       Residual Block       #####
######################################


def create_layer(generator: torch.Generator,
                layer_name:str, 
                in_dim:int, 
                out_dim:int,
                activation: Optional[nn.Module],
                sampling_method: str = "gradient",
                ):
    if layer_name == "dense":
        return Dense(generator, in_dim, out_dim, activation)
    elif layer_name == "SWIM":
        return SWIMLayer(generator, in_dim, out_dim, activation, sampling_method)
    elif layer_name == "identity":
        return Identity()
    else:
        raise ValueError(f"layer_name must be one of ['dense', 'SWIM', 'identity']. Given: {layer_name}")


class ResidualBlock(FittableModule):
    def __init__(self, 
                 generator: torch.Generator,
                 in_dim: int,
                 bottleneck_dim: int,
                 layer1: str,
                 layer2: str,
                 activation: nn.Module = nn.Tanh(),
                 residual_scale: float = 1.0,
                 sampling_method: Literal['uniform', 'gradient'] = 'gradient',
                 ):
        """Residual block with 2 layers and a skip connection.
        
        Args:
            generator (torch.Generator): PRNG object.
            in_dim (int): Input dimension.
            bottleneck_dim (int): Dimension of the bottleneck layer.
            layer1 (str): First layer in the block. One of ["dense", "swim", "identity"].
            layer2 (str): See layer1.
            activation (nn.Module): Activation function.
            residual_scale (float): Scale of the residual connection.
            sampling_method (str): Pair sampling method for SWIM. One of ['uniform', 'gradient'].
        """
        super(ResidualBlock, self).__init__()
        self.residual_scale = residual_scale
        self.first = create_layer(generator, layer1, in_dim, bottleneck_dim, None, sampling_method)
        self.activation = activation
        self.second = create_layer(generator, layer2, bottleneck_dim, in_dim, None, sampling_method)


    def fit(self, X: Tensor, y: Tensor) -> Tuple[Tensor, Tensor]:
        with torch.no_grad():
            X0 = X
            X, y = self.first.fit(X,y)
            X = self.activation(X)
            X, y = self.second.fit(X,y)
        return X0 + X * self.residual_scale, y


    def forward(self, X: Tensor) -> Tensor:
        X0 = X
        X = self.first(X)
        X = self.activation(X)
        X = self.second(X)
        return X0 + X * self.residual_scale
    

#####################################
##### Residual Networks         #####
##### - ResNet                  #####
##### - NeuralEulerODE          #####
#####################################

class ResNet(Sequential):
    def __init__(self, 
                 generator: torch.Generator,
                 in_dim: int,
                 hidden_size: int,
                 bottleneck_dim: int,
                 n_blocks: int,
                 upsample_layer: Literal['dense', 'SWIM', 'identity'] = 'SWIM',
                 upsample_activation: nn.Module = nn.Tanh(),
                 res_layer1: str = "SWIM",
                 res_layer2: str = "dense",
                 res_activation: nn.Module = nn.Tanh(),
                 residual_scale: float = 1.0,
                 sampling_method: Literal['uniform', 'gradient'] = 'gradient',
                 output_layer: Literal['ridge', 'dense', 'identity', 'logistic regression'] = 'ridge',
                 ):
        """Residual network with multiple residual blocks.
        
        Args:
            generator (torch.Generator): PRNG object.
            in_dim (int): Input dimension.
            hidden_size (int): Dimension of the hidden layers.
            bottleneck_dim (int): Dimension of the bottleneck layer.
            n_blocks (int): Number of residual blocks.
            upsample_layer (str): Layer before any residual connections. One of ['dense', 'SWIM', 'identity'].
            upsample_activation (nn.Module): Activation function for the upsample layer.
            res_layer1 (str): First layer in the block. One of ["dense", "swim", "identity"].
            res_layer2 (str): See layer1.
            res_activation (nn.Module): Activation function for the residual blocks.
            residual_scale (float): Scale of the residual connection.
            sampling_method (str): Pair sampling method for SWIM. One of ['uniform', 'gradient'].
            output_layer (str): Output layer. One of ['ridge', 'ridge classifier', 'dense', 'identity', 'logistic regression'].
        """
        upsample = create_layer(generator, 
                                     upsample_layer, 
                                     in_dim, 
                                     hidden_size, 
                                     upsample_activation, 
                                     sampling_method)
        residual_blocks = [
            ResidualBlock(generator, 
                          hidden_size, 
                          bottleneck_dim, 
                          res_layer1, 
                          res_layer2, 
                          res_activation, 
                          residual_scale, 
                          sampling_method)
            for _ in range(n_blocks)
        ]
        if output_layer == 'dense':
            out = Dense(generator, hidden_size, 1, None)
        elif output_layer == 'ridge':
            out = RidgeCVModule()
        elif output_layer == 'ridge classifier':
            out = RidgeClassifierCVModule()
        elif output_layer == 'identity':
            out = Identity()
        elif output_layer == 'logistic regression':
            out = LogisticRegressionModule(generator)
        else:
            raise ValueError(f"output_layer must be one of ['ridge', 'ridge classifier', 'dense', 'identity', 'logistic regression']. Given: {output_layer}")
        
        super(ResNet, self).__init__(upsample, *residual_blocks, out)



class NeuralEulerODE(ResNet):
    def __init__(self, 
                 generator: torch.Generator,
                 in_dim: int,
                 hidden_size: int,
                 n_layers: int,
                 upsample_layer: Literal['dense', 'SWIM', 'identity'] = 'SWIM',
                 upsample_activation: nn.Module = nn.Tanh(),
                 res_layer: str = "SWIM",
                 res_activation: nn.Module = nn.Tanh(),
                 residual_scale: float = 1.0,
                 sampling_method: Literal['uniform', 'gradient'] = 'gradient',
                 output_layer: Literal['ridge', 'dense'] = 'dense',
                 ):
        """Euler discretization of Neural ODE."""
        super(NeuralEulerODE, self).__init__(generator, in_dim, hidden_size, None,
                                             n_layers, upsample_layer, upsample_activation,
                                             res_layer, "identity", res_activation,
                                             residual_scale, sampling_method, output_layer)


######################################
##### Trainers                   #####
##### - AdamTrainer              #####
######################################


def kaiming_normal_with_generator(weight, generator=None):
    fan_in = weight.size(1)
    std = (2.0 / fan_in) ** 0.5
    nn.init.normal_(weight, mean=0, std=std, generator=generator)


class E2EResNet(FittableModule):
    def __init__(self, 
                 generator: torch.Generator,
                 in_dim: int,
                 hidden_size: int,
                 bottleneck_dim: int,
                 out_dim: int,
                 n_blocks: int,
                 activation: nn.Module = nn.Tanh(),
                 loss: nn.Module = nn.MSELoss(),
                 lr: float = 1e-3,
                 epochs: int = 10,
                 weight_decay: float = 1e-5,
                 batch_size: int = 64,
                 ):
        """End-to-end trainer for residual networks using Adam optimizer with Batch Normalization.
        
        Args:
            generator (torch.Generator): PRNG object.
            in_dim (int): Input dimension.
            hidden_size (int): Dimension of the hidden layers.
            bottleneck_dim (int): Dimension of the bottleneck layer.
            out_dim (int): Output dimension.
            n_blocks (int): Number of residual blocks.
            activation (nn.Module): Activation function.
            loss (nn.Module): Loss function.
            lr (float): Learning rate for Adam optimizer.
            epochs (int): Number of training epochs.
            weight_decay (float): Weight decay for Adam optimizer.
            batch_size (int): Batch size for training.
        """
        super(E2EResNet, self).__init__()
        self.generator = generator
        self.epochs = epochs
        self.batch_size = batch_size

        # Define resnet with batch norm
        self.upsample = nn.Linear(in_dim, hidden_size)
        self.batch_norm = nn.BatchNorm1d(hidden_size)
        self.activation = activation
        
        self.residual_blocks = nn.ModuleList([
            nn.Sequential(
                nn.Linear(hidden_size, bottleneck_dim),
                nn.BatchNorm1d(bottleneck_dim),
                activation,
                nn.Linear(bottleneck_dim, hidden_size),
                nn.BatchNorm1d(hidden_size)
            ) for _ in range(n_blocks)
        ])
        self.output_layer = nn.Linear(hidden_size, out_dim)

        # Optimizer and loss
        self.loss = loss
        self.optimizer = torch.optim.Adam(self.parameters(), lr=lr, weight_decay=weight_decay)


    def fit(self, X: Tensor, y: Tensor):
        """Trains network end to end with Adam optimizer and a tabular data loader"""
        device = X.device
        self.to(device)

        # Initialize weights for residual blocks with generator
        kaiming_normal_with_generator(self.upsample.weight, self.generator)
        nn.init.zeros_(self.upsample.bias)
        for block in self.residual_blocks:
            for layer in block:
                if isinstance(layer, nn.Linear):
                    kaiming_normal_with_generator(layer.weight, self.generator)
                    nn.init.zeros_(layer.bias)
        kaiming_normal_with_generator(self.output_layer.weight, self.generator)
        nn.init.zeros_(self.output_layer.bias)

        # Create a CPU generator for DataLoader
        data_loader_generator = torch.Generator(device='cpu')
        data_loader_generator.manual_seed(self.generator.initial_seed())
        dataset = torch.utils.data.TensorDataset(X, y)
        loader = torch.utils.data.DataLoader(
            dataset, 
            batch_size=self.batch_size, 
            shuffle=True, 
            generator=data_loader_generator
        )

        # training loop
        for epoch in tqdm(range(self.epochs)):
            for batch_X, batch_y in loader:
                # batch_X and batch_y are already on the device
                self.optimizer.zero_grad()
                outputs = self(batch_X)
                loss = self.loss(outputs, batch_y)
                loss.backward()
                self.optimizer.step()

        return self(X), y


    def forward(self, X: Tensor) -> Tensor:
        X = self.upsample(X)
        X = self.batch_norm(X)
        X = self.activation(X)
        for block in self.residual_blocks:
            X = X + block(X)
        X = self.output_layer(X)
        return X




class RandFeatBoost(FittableModule):
    def __init__(self, 
                 generator: torch.Generator, 
                 in_dim: int = 1,
                 hidden_size: int = 128, 
                 out_dim: int = 1,
                 n_blocks: int = 5,
                 activation: nn.Module = nn.Tanh(),
                 loss_fn: nn.Module = nn.MSELoss(),
                 adam_lr: float = 1e-3,
                 boost_lr: float = 1.0,
                 epochs: int = 50,
                 batch_size: int = 64,
                 upscale_type = "SWIM", # "dense", identity
                 second_in_resblock = "identity",
                 ):
        super(RandFeatBoost, self).__init__()
        self.generator = generator
        self.hidden_size = hidden_size
        self.out_dim = out_dim
        self.n_blocks = n_blocks
        self.activation = activation
        self.loss_fn = loss_fn
        self.adam_lr = adam_lr
        self.boost_lr = boost_lr
        self.epochs = epochs
        self.batch_size = batch_size
        self.upscale_type = upscale_type
        self.second_in_resblock = second_in_resblock

        self.upscale = create_layer(generator, upscale_type, in_dim, hidden_size, activation)
        self.layers = []
        self.deltas = []
        self.classifiers = []
        self.classifier = None #TODO currently only support logistic regression kind of


    def fit(self, X: Tensor, y: Tensor):
        device = X.device
        X0 = X
        X, y = self.upscale.fit(X, y)

        # Create a CPU generator for DataLoader
        data_loader_generator = torch.Generator(device='cpu')
        data_loader_generator.manual_seed(self.generator.initial_seed())

        # Layerwise boosting
        for t in range(self.n_blocks):
            layer = ResidualBlock(self.generator, self.hidden_size, self.hidden_size, self.upscale_type, self.second_in_resblock, self.activation)
            layer.fit(X, y)

            # Create top classifier
            classifier = nn.Linear(self.hidden_size, self.out_dim).to(device)
            #DELTA = nn.Parameter(torch.zeros(1, self.hidden_size, device=device))
            DELTA = nn.Parameter(torch.zeros(1, 1, device=device))
            if t > 0:
                classifier.weight.data = self.classifiers[-1].weight.data.clone()
                classifier.bias.data = self.classifiers[-1].bias.data.clone()

            #data loader
            dataset = torch.utils.data.TensorDataset(X, y)
            loader = torch.utils.data.DataLoader(
                dataset, 
                batch_size=self.batch_size, 
                shuffle=True, 
                generator=data_loader_generator
            )

            #learn top level classifier and boost
            params = list(classifier.parameters()) + [DELTA]
            self.optimizer = torch.optim.Adam(params, lr=self.adam_lr, weight_decay=1e-5)
            for epoch in tqdm(range(self.epochs)):
                for batch_X, batch_y in loader:
                    self.optimizer.zero_grad()

                    #forward pass
                    FofX = layer(batch_X) - batch_X # due to how i programmed ResidualBlock...
                    outputs = classifier(batch_X + DELTA * FofX)

                    #loss and backprop
                    loss = self.loss_fn(outputs, batch_y)
                    loss.backward()
                    self.optimizer.step()
            
            #after convergence, update layers, deltas, and X
            self.layers.append(layer)
            self.deltas.append(DELTA)
            self.classifiers.append(classifier)
            with torch.no_grad():
                X = X + self.boost_lr * DELTA * (layer(X)-X)

        self.classifier = classifier
        return self(X0), y


    def forward(self, X: Tensor) -> Tensor:
        X = self.upscale(X)
        for layer, DELTA in zip(self.layers, self.deltas):
            FofX = layer(X) - X
            X = X + self.boost_lr * DELTA * FofX
        return self.classifier(X)