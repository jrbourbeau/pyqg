import numpy as np
from numpy import pi
try:   
    import mkl
    np.use_fastnumpy = True
except ImportError:
    pass

try:
    import pyfftw
    pyfftw.interfaces.cache.enable() 
except ImportError:
    pass

class QGModel(object):
    """A class that represents the two-layer QG model."""
    
    def __init__(
        self,
        # grid size parameters
        nx=64,                     # grid resolution
        ny=None,
        L=1e6,                     # domain size is L [m]
        W=None,
        # physical parameters
        beta=1.5e-11,               # gradient of coriolis parameter
        rek=5.787e-7,               # linear drag in lower layer
        rd=15000.0,                 # deformation radius
        delta=0.25,                 # layer thickness ratio (H1/H2)
        U1=0.025,                   # upper layer flow
        U2=0.0,                     # lower layer flow
        # timestepping parameters
        dt=7200.,                   # numerical timestep
        tplot=10000.,               # interval for plots (in timesteps)
        tcfl=1000.,                 # interval for cfl writeout (in timesteps)
        tmax=1576800000.,           # total time of integration
        tavestart=315360000.,       # start time for averaging
        taveint=86400.,             # time interval used for summation in longterm average in seconds
        tpickup=31536000.,          # time interval to write out pickup fields ("experimental")
        # diagnostics parameters
        diagnostics_list='all',     # which diagnostics to output
        # fft parameter
        fftw = False,               # fftw flag 
        ntd = 3,                    # number of threads to use in fftw computations
        ):
        """Initialize the two-layer QG model.
        
        The model parameters are passed as keyword arguments.
        They are grouped into the following categories
        
        Grid Parameter Keyword Arguments:
        nx -- number of grid points in the x direction
        ny -- number of grid points in the y direction (default: nx)
        L -- domain length in x direction, units meters 
        W -- domain width in y direction, units meters (default: L)
        (WARNING: some parts of the model or diagnostics might
        actuallye assume nx=ny -- check before making different choice!)
        
        Physical Paremeter Keyword Arguments:
        beta -- gradient of coriolis parameter, units m^-1 s^-1
        rek -- linear drag in lower layer, units seconds^-1
        rd -- deformation radius, units meters
        delta -- layer thickness ratio (H1/H2)
        (NOTE: currently some diagnostics assume delta==1)
        U1 -- upper layer flow, units m/s
        U2 -- lower layer flow, units m/s
        
        Timestep-related Keyword Arguments:
        dt -- numerical timstep, units seconds
        tplot -- interval for plotting, units number of timesteps
        tcfl -- interval for cfl writeout, units number of timesteps
        tmax -- total time of integration, units seconds
        tavestart -- start time for averaging, units seconds
        tsnapstart -- start time for snapshot writeout, units seconds
        taveint -- time interval for summation in diagnostic averages,
                   units seconds
           (for performance purposes, averaging does not have to
            occur every timestep)
        tsnapint -- time interval for snapshots, units seconds 
        tpickup -- time interval for writing pickup files, units seconds
        (NOTE: all time intervals will be rounded to nearest dt interval)
        """

        if ny is None: ny = nx
        if W is None: W = L
       
        # put all the parameters into the object
        # grid
        self.nx = nx
        self.ny = ny
        self.L = L
        self.W = W
        # physical
        self.beta = beta
        self.rek = rek
        self.rd = rd
        self.delta = delta
        self.U1 = U1
        self.U2 = U2
        # timestepping
        self.dt = dt
        self.tplot = tplot
        self.tcfl = tcfl
        self.tmax = tmax
        self.tavestart = tavestart
        self.taveint = taveint
        self.tpickup = tpickup
        # fft 
        self.fftw = fftw
        self.ntd = ntd

        # compute timestep stuff
        self.taveints = np.ceil(taveint/dt)      

        self.x,self.y = np.meshgrid(
            np.arange(0.5,self.nx,1.)/self.nx*self.L,
            np.arange(0.5,self.ny,1.)/self.ny*self.W )
        
        # initial conditions: (PV anomalies)
        self.set_q1q2(
            1e-7*np.random.rand(self.ny,self.nx) + 1e-6*(
                                np.ones((self.ny,1)) * np.random.rand(1,self.nx) ),
            np.zeros_like(self.x)        
        )   

        # Background zonal flow (m/s):
        self.U = self.U1 - self.U2

        # Notice: at xi=1 U=beta*rd^2 = c for xi>1 => U>c

        # wavenumber one
        self.k0x = 2.*pi/self.L
        self.k0y = 2.*pi/self.W
        # wavenumber grids
        
        self.ll = (2.*pi/self.W)*np.append( np.arange(0.,self.nx/2), \
            np.arange(-self.nx/2,0.) )
        self.kk = (2.*pi/self.L)*np.arange(0.,self.nx/2+1)

        self.k, self.l = np.meshgrid(self.kk, self.ll)
        # physical grid spacing
        self.dx = self.L / self.nx
        self.dy = self.W / self.ny

        # the F parameters
        self.F1 = self.rd**-2 / (1.+self.delta)
        self.F2 = self.delta*self.F1

        # the meridional PV gradients in each layer
        self.beta1 = self.beta + self.F1*(self.U1 - self.U2)
        self.beta2 = self.beta - self.F2*(self.U1 - self.U2)

        self.del1 = self.delta/(self.delta+1.)
        self.del2 = (self.delta+1.)**-1

        # isotropic wavenumber^2 grid
        self.wv2 = np.ma.masked_equal(self.k**2 + self.l**2, 0.).filled(1.e20)
        self.wv2i = self.wv2**-2
        
        # determine inversion matrix: psi = A q (i.e. A=M_2**(-1) where q=M_2*psi)
        det = self.wv2 * (self.wv2 + self.F1 + self.F2)
        self.a11 = -((self.wv2 + self.F2)/det)
        self.a12 = -((self.F1)/det)
        self.a21 = -((self.F2)/det)
        self.a22 = -((self.wv2 + self.F1)/det)

        self.det = det

        # this defines the spectral filter (following Arbic and Flierl, 2003)
        cphi=0.65*pi
        wvx=np.sqrt((self.k*self.dx)**2.+(self.l*self.dy)**2.)
        self.filtr = np.exp(-23.6*(wvx-cphi)**4.)     
        self.filtr[wvx<=cphi] = 1.                   


        # initialize timestep
        self.t=0        # actual time
        self.tc=0       # timestep number
        
        # Set time-stepping parameters for very first timestep (Euler-forward stepping).
        # Second-order Adams Bashford used thereafter and is set up at the end of the first time-step (see below)
        self.dqh1dt_p = 0.
        self.dqh2dt_p = 0.
        self.dt0 = self.dt
        self.dt1 = 0.
        
        self._initialize_diagnostics()
        if diagnostics_list == 'all':
            pass # by default, all diagnostics are active
        elif diagnostics_list == 'none':
            self.set_active_diagnostics([])
        else:
            self.set_active_diagnostics(diagnostics_list)

    def set_q1q2(self, q1, q2):
        self.q1 = q1
        self.q2 = q2

        # initialize spectral PV
        self.qh1 = fft2(self, self.q1)
        self.qh2 = fft2(self, self.q2) 

    # compute advection in grid space (returns qdot in fourier space)
    def advect(self, q, u, v):
        return 1j*self.k*fft2(self, u*q) + 1j*self.l*fft2(self, v*q)
        
    # compute grid space u and v from fourier streafunctions
    def caluv(self, ph):
        u = -ifft2(self, 1j*self.l*ph)
        v = ifft2(self, 1j*self.k*ph)
        return u, v
  
    # Invert PV for streamfunction
    def invph(self, zh1, zh2):
        ph1 = self.a11*zh1 + self.a12*zh2
        ph2 = self.a21*zh1 + self.a22*zh2
        return ph1, ph2
    
    def run_with_snapshots(self, tsnapstart=0., tsnapint=432000.):
        """Run the model forward until the next snapshot, then yield."""
        
        tsnapints = np.ceil(tsnapint/self.dt)
        nt = np.ceil(np.floor((self.tmax-tsnapstart)/self.dt+1)/tsnapints)
        
        while(self.t < self.tmax):
            self._step_forward()
            if self.t>=tsnapstart and (self.tc%tsnapints)==0:
                yield self.t
        return
                
    def run(self):
        """Run the model forward without stopping until the end."""
        while(self.t < self.tmax): 
            self._step_forward()

            if np.isnan(self.calc_ke()):
                print " *** Blow up  "
                break

    def _step_forward(self):
        # compute grid space qgpv
        self.q1 = ifft2(self, self.qh1)
        self.q2 = ifft2(self, self.qh2)

        # invert qgpv to find streamfunction and velocity
        self.ph1, self.ph2 = self.invph(self.qh1, self.qh2)
        self.u1, self.v1 = self.caluv(self.ph1)
        self.u2, self.v2 = self.caluv(self.ph2)
        
        # here is where we calculate diagnostics
        if (self.t>=self.dt) and (self.tc%self.taveints==0):
            self._increment_diagnostics()

        # check cfl
        if (self.tc % self.tcfl)==0:
            print 't=%16d, tc=%10d: cfl=%5.6f, ke=%5.6f' % (
                   self.t, self.tc, self.calc_cfl(), \
                           self.calc_ke() )
        
        # compute tendency from advection and bottom drag:  
        self.dqh1dt = (-self.advect(self.q1, self.u1 + self.U1, self.v1)
                  -self.beta1*1j*self.k*self.ph1)
        self.dqh2dt = (-self.advect(self.q2, self.u2 + self.U2, self.v2)
                  -self.beta2*1j*self.k*self.ph2 + self.rek*self.wv2*self.ph2)
              
        # add time tendencies (using Adams-Bashforth):
        self.qh1 = self.filtr*(
                    self.qh1 + self.dt0*self.dqh1dt + self.dt1*self.dqh1dt_p)
        self.qh2 = self.filtr*(
                    self.qh2 + self.dt0*self.dqh2dt + self.dt1*self.dqh2dt_p)  
        
        # remember previous tendencies
        self.dqh1dt_p = self.dqh1dt.copy()
        self.dqh2dt_p = self.dqh2dt.copy()
        
        # the actual Adams-Bashforth stepping can only be used starting
        # at the second time-step and is thus set here:   
        if self.tc==0:
            self.dt0 = 1.5*self.dt
            self.dt1 = -0.5*self.dt
        
        # augment timestep
        self.tc += 1
        self.t += self.dt
    
    def calc_cfl(self):
        return np.abs(np.hstack([self.u1 + self.U1, self.v1,
                          self.u2 + self.U2, self.v2])).max()*self.dt/self.dx

    def calc_ke(self):
        return  ( 2.*0.5*self.wv2*( np.abs(self.ph1)**2 + \
                np.abs(self.ph2)**2 )/ ( (self.nx*self.ny)**2) ).sum()

    ### All the diagnostic stuff follows. ###

    def set_active_diagnostics(self, diagnostics_list):
        for d in self.diagnostics:
            self.diagnostics[d]['active'] == (d in diagnostics_list)

    def _initialize_diagnostics(self):
        # Initialization for diagnotics
        self.diagnostics = dict()

        self.add_diagnostic('entspec',
            description='barotropic enstrophy spectrum',
            function= (lambda self:
                      np.abs(self.del1*self.qh1 + self.del2*self.qh2)**2.)
        )
            
        self.add_diagnostic('APEflux',
            description='spectral flux of available potential energy',
            function= (lambda self:
              self.rd**-2 * self.del1*self.del2 *
              np.real((self.ph1-self.ph2)*np.conj(self.Jptpc)) )

        )
        
        self.add_diagnostic('KEflux',
            description='spectral flux of kinetic energy',
            function= (lambda self:
              np.real(self.del1*self.ph1*np.conj(self.Jp1xi1)) + 
              np.real(self.del2*self.ph2*np.conj(self.Jp2xi2)) )
        )

        self.add_diagnostic('KE1spec',
            description='upper layer kinetic energy spectrum',
            function=(lambda self: 0.5*self.wv2*np.abs(self.ph1)**2)
        )
        
        self.add_diagnostic('KE2spec',
            description='lower layer kinetic energy spectrum',
            function=(lambda self: 0.5*self.wv2*np.abs(self.ph2)**2)
        )
        
        self.add_diagnostic('q1',
            description='upper layer QGPV',
            function= (lambda self: self.q1)
        )

        self.add_diagnostic('q2',
            description='lower layer QGPV',
            function= (lambda self: self.q2)
        )

        self.add_diagnostic('EKE1',
            description='mean upper layer eddy kinetic energy',
            function= (lambda self: 0.5*(self.v1**2 + self.u1**2).mean())
        )

        self.add_diagnostic('EKE2',
            description='mean lower layer eddy kinetic energy',
            function= (lambda self: 0.5*(self.v2**2 + self.u2**2).mean())
        )
        
        self.add_diagnostic('EKEdiss',
            description='total energy dissipation by bottom drag',
            function= (lambda self:
                       (self.del2*self.rek*self.wv2*
                        np.abs(self.ph2)**2./(self.nx*self.ny)).sum())
        )
        
        self.add_diagnostic('APEgenspec',
            description='spectrum of APE generation',
            function= (lambda self: self.U * self.rd**-2 * self.del1 * self.del2 *
                       np.real(1j*self.k*(self.del1*self.ph1 + self.del2*self.ph2) *
                                  np.conj(self.ph1 - self.ph2)) )
        )
        
        self.add_diagnostic('APEgen',
            description='total APE generation',
            function= (lambda self: self.U * self.rd**-2 * self.del1 * self.del2 *
                       np.real(1j*self.k*
                           (self.del1*self.ph1 + self.del2*self.ph2) *
                            np.conj(self.ph1 - self.ph2)).sum() / 
                            (self.nx*self.ny) )
        )

    def add_diagnostic(self, diag_name, description=None, units=None, function=None):
        # create a new diagnostic dict and add it to the object array
        
        # make sure the function is callable
        assert hasattr(function, '__call__')
        
        # make sure the name is valid
        assert isinstance(diag_name, str)
        
        # by default, diagnostic is active
        self.diagnostics[diag_name] = {
           'description': description,
           'units': units,
           'active': True,
           'count': 0,
           'function': function, }
           
    def _increment_diagnostics(self):
        # compute intermediate quantities needed for some diagnostics
        self.p1 = ifft2(self, self.ph1)
        self.p2 = ifft2(self, self.ph2)
        self.xi1 = ifft2(self, -self.wv2*self.ph1)
        self.xi2 = ifft2(self, -self.wv2*self.ph2)
        self.Jptpc = -self.advect(
                    (self.p1 - self.p2),
                    (self.del1*self.u1 + self.del2*self.u2),
                    (self.del1*self.v1 + self.del2*self.v2))
        # fix for delta.neq.1
        self.Jp1xi1 = self.advect(self.xi1, self.u1, self.v1)
        self.Jp2xi2 = self.advect(self.xi2, self.u2, self.v2)
        
        for dname in self.diagnostics:
            if self.diagnostics[dname]['active']:
                res = self.diagnostics[dname]['function'](self)
                if self.diagnostics[dname]['count']==0:
                    self.diagnostics[dname]['value'] = res
                else:
                    self.diagnostics[dname]['value'] += res
                self.diagnostics[dname]['count'] += 1
                
    def get_diagnostic(self, dname):
        return (self.diagnostics[dname]['value'] / 
                self.diagnostics[dname]['count'])


# DFT functions
def fft2(cself, a):
    if cself.fftw:
        aw = pyfftw.n_byte_align_empty(a.shape, 8, 'float64')
        aw[:]= a.copy()
        return pyfftw.builders.rfft2(aw,threads=cself.ntd)()
    else:
        return np.fft.rfft2(a)

def ifft2(cself, ah):
    if cself.fftw:
        awh = pyfftw.n_byte_align_empty(ah.shape, 16, 'complex128')
        awh[:]= ah.copy()
        return pyfftw.builders.irfft2(awh,threads=cself.ntd)()
    else:
        return np.fft.irfft2(ah)

