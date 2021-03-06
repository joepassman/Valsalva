import numpy
import math
import enum
from scipy.signal import savgol_filter
import numpy
import scipy.signal as ss
import os
import re
import matplotlib.pyplot as plt
from typing import Iterable
import importlib.util
import inspect

# For variance based cost function we need a guess of variance of any target variables
# does not really work for timed variables though
DEFAULT_STD_PERCENT = 10

class SimulationResultIncompleteError(ValueError):
    def __init__(self, length:float, minimalLength:float, owner:str):
        m = 'Incomplete simulation output, the simulation probably crashed at %.2f s (%.1f s required). At %s' % (length, minimalLength, owner)
        super().__init__(m)

class CostFunctionType(enum.Enum):
    Ignore = 0
    Quadratic = 1
    QuadraticVariance = 2
    Linear = 3
    LinearVariance = 4
    DistanceFromZero = 5
    

class ObjectiveVar:
    """
    Provides objects and functions for calculating cost functions

    """

    def __init__(self, name, 
                value=None, 
                targetValue=None, 
                limit=None, 
                weight=1, 
                k_p=1e3, 
                std = None,
                costFunctionType=CostFunctionType.Quadratic):
        self.name = name
        self.targetValue = targetValue
        self.value = value
        self.limit = limit if limit is not None else [-math.inf, math.inf]
        self.weight = weight
        # self.costLimit = -1 # unlimited costs
        self.k_p = k_p # multiplier for out ouf limit values
        self.costFunctionType = costFunctionType
        # standard deviation
        self.std = std

    def __cost_function(self, measured, target):
        # calculate costs. Could go negative or NaN for negative or zero measured values!
        if self.costFunctionType is CostFunctionType.Quadratic:
            if target is None:
                return 0
            return self.weight*(measured - target)**2/(target**2)

        elif self.costFunctionType is CostFunctionType.QuadraticVariance:
            if target is None:
                return 0            
            if self.std is None:
                self.std = abs(target*DEFAULT_STD_PERCENT/100)
            # variance is squared standard deviation
            return self.weight*(measured - target)**2/(target*self.std)

        elif self.costFunctionType is CostFunctionType.Linear:
            if target is None:
                return 0
            return self.weight*abs(measured - target)/abs(target)

        elif self.costFunctionType is CostFunctionType.LinearVariance:
            if target is None:
                return 0
            if self.std is None:
                self.std = abs(target*DEFAULT_STD_PERCENT/100)
            # variance is squared standard deviation
            return self.weight*abs(measured - target)/(self.std)

        elif self.costFunctionType is CostFunctionType.DistanceFromZero:
            return self.weight*abs(measured)

        elif self.costFunctionType is CostFunctionType.Ignore:
            return 0
        else:
            raise NotImplementedError()



    def cost(self):
        """
        Calculates costs per variable as difference from target value and applies steep linear penalty for outside of the bounds values, if defined.
        """
        measured = self.value
        c = self.__cost_function(measured, self.targetValue)

        min_val = self.limit[0]
        max_val = self.limit[1]

        if measured < min_val:
            return c + self.__cost_function(measured, min_val)*self.k_p
        elif measured > max_val:
            return c + self.__cost_function(measured, max_val)*self.k_p
        else:
            return c
    
    def inLimit(self):
        if self.value < self.limit[0] or self.value > self.limit[1]:
            return False
        else:
            return True

    def target_log(self):
        if self.costFunctionType is CostFunctionType.DistanceFromZero:
            target = "%d" % 0
        elif self.targetValue  is not None:
            target = "%.3e" % self.targetValue
        else:
            target = ' in limit' if self.inLimit() else 'out limit'
        
        return target

    def __repr__(self):
        return '%s,%.3e,%s' % (self.name, self.value, self.target_log())


def getObjectiveByName(objective_iterable, name) -> ObjectiveVar:
    """
    Returns objective of given name from the list
    """
    return next((i for i in objective_iterable if i.name == name))

def findInterval(t_from, t_to, timeArr):
    return range(findLowestIndex(t_from, timeArr), findLowestIndex(t_to, timeArr))

def findLowestIndex(time, timeArr):
    lst = timeArr.tolist()
    return next((i for i, x in enumerate(lst) if x >= time))


def avg(var_set, interval):
    return sum(var_set[interval]) /len(interval)

def calculatePWV(timeArr, signal, delayedSignal, distance):
    """
    Calculates pulse wave propagation velocity based on bottom base shift of pressure signals.
    In other words, it compares  position of signals minimum

    This is the simpler approach, however it does not work in all cases.
    """    
    # find last three seconds - should do for as low as 30 bpm
    # we better leave some time to find min of the other signal
    i_from = findLowestIndex(timeArr[-1] - 3, timeArr)
    i_to = findLowestIndex(timeArr[-1] - 1, timeArr)

    # signal window
    sig_win = signal[i_from:i_to].tolist()
    i_first_min = sig_win.index(min(sig_win)) + i_from
    t_first_min = timeArr[i_first_min]

    # 2 m/s is an absolute lazy crazy slow velocity to cut the second signal window with
    max_t = distance / 2
    i_second_window = findLowestIndex(t_first_min + max_t, timeArr)
    # second window is shifted from the first one and substantially shorter
    delayedSignal_window = delayedSignal[i_first_min:i_second_window].tolist()
    i_second_min = i_first_min + delayedSignal_window.index(min(delayedSignal_window))
    t_second_min = timeArr[i_second_min]

    timediff = t_second_min - t_first_min
    velocity = distance/timediff
    return velocity

def calculatePWV2(timeArr, signal, delayedSignal, distance):
    """
    More robust way to calculate PWV takes position of maximal signal rising steepness.
    In other words, takes maxs of difference

    """

    diff_signal = numpy.diff(signal)
    diff_delayedSignal = numpy.diff(delayedSignal)
    
    # call with signal derivatives - maxing the steepness
    # we have to use negative though, as calculatePWV is taking mins instead of max
    return calculatePWV(timeArr, numpy.negative(diff_signal), numpy.negative(diff_delayedSignal), distance)

def calculateEF(volumes):
    esv = min(volumes)
    edv = max(volumes)
    return (edv - esv)/edv

def calculateQ_MV(q, time, interval):
    """ Returns a fraction of passive and atrial active(second bump) heart filling rate

    q : flow signal
    time : time set
    interval : guess range interval in which to search. May overflow during search, so leave at least one beat buffer.

    """
    # find the first peak in the interval
    max_index = numpy.argmax(q[interval]) + interval[0]

    # find an one beat interval - lloking for first zero or negative flow (closed valve)
    min_index = next(i for  i, v in enumerate(q[max_index:]) if v <= 0) + max_index

    # get the peak in reverse direction
    m = 0
    for v in reversed(q[max_index:min_index]):
        # find a maximum
        m = max(m, v)
        # detect a decrease by 10% from max and stop right there, before hitting the main peak
        if v <= 0.9*m:
            break
    
    return q[max_index]/m
    
    

def getOddWindow(time, dt):
    win = round(time/dt)
    return int(win) if win%2 == 1 else int(win) + 1

def detrend(sig, window, cutoff = -math.inf):
    sigf = savgol_filter(sig, window, 3)
    return [max(s - sf, cutoff) for s, sf in zip(sig, sigf)]

def getPeaks(sig, dt):
    # get mean and detrend
    win = getOddWindow(2, dt)
    sigDet = detrend(sig, win, 0)
    # and again
    sigDet2 = detrend(sigDet, win, 0)
    # plt.plot(sigDet)
    # plt.plot(sigDet2)
    # plt.show()

    # find peaks - its minimal distance is 0.3 s (is about 200 BPM) and is above the mean of the signal
    peaks, _ = ss.find_peaks(sigDet2, distance= int(0.4/dt))
    return peaks

def getHR(sig, peaks, dt):
    
    heart_rate = [0]*len(sig)
    for i in range(1, len(peaks)):
        # loop from 2nd
        # take range inbetween the means
        rng = slice(peaks[i-1], peaks[i], 1)
        # time between the peaks
        beat_time = (peaks[i] - peaks[i-1])*dt
        heart_rate[rng] = [1/beat_time]*(rng.stop-rng.start)
     
    # before the first peak, lets fill with the first value
    heart_rate[0: peaks[0]] = [heart_rate[peaks[0]]]*peaks[0]

    # fill in the last
    l = len(heart_rate[peaks[-1]:] )
    heart_rate[peaks[-1]:] = [heart_rate[peaks[-1]-1]]*l

    return numpy.array(heart_rate)
    

def getMeanRR(sig, peaks):

    means = [0]*len(sig)
    for i in range(1, len(peaks)):
        # loop from 2nd
        # take range inbetween the means
        rng = slice(peaks[i-1], peaks[i], 1)
        # take mean from two peaks
        means[rng] = [sig[rng].mean()]*(rng.stop-rng.start)
    
    # fill in the begining - value of first peak, long up to first peak indice
    means[0: peaks[0]] = [means[peaks[0]]]*peaks[0]

    # fill in the last
    l = len(means[peaks[-1]:] )
    means[peaks[-1]:] = [means[peaks[-1]-1]]*l

    return numpy.array(means)

def getPPulseRR(sig, peaks):
    """ Returns pulse pressure inbetween the peaks """

    pp = [0]*len(sig)
    for i in range(1, len(peaks)):
        # loop from 2nd
        # take range inbetween the means
        rng = slice(peaks[i-1], peaks[i], 1)
        # take mean from two peaks
        pp[rng] = [sig[rng].max() - sig[rng].min()]*(rng.stop-rng.start)
    
    # fill in the begining - value of first peak, long up to first peak indice
    pp[0: peaks[0]] = [pp[peaks[0]]]*peaks[0]

    # fill in the last
    l = len(pp[peaks[-1]:] )
    pp[peaks[-1]:] = [pp[peaks[-1]-1]]*l

    return numpy.array(pp)

def getValsalvaStart(time, thoracic_pressure, threshold = 1330):
    
    return time[findLowestIndex(threshold, thoracic_pressure)]

def getValsalvaEnd(valsalva_start, time, thoracic_pressure, min_length = 5, threshold = 1330):
    # minimal valsalva length is 5
    i_from = findLowestIndex(valsalva_start + min_length, time)
    # get rid of noise for thresholding
    tpf = ss.medfilt(thoracic_pressure, 7)
    
    i = next((i for i, x in numpy.ndenumerate(tpf[i_from:]) if x <= threshold))
    return time[i] + valsalva_start + min_length

def updateObjectivesByValuesFromFile(filename, objectives = None) -> Iterable[ObjectiveVar]:
    """
    If no objectives are provided, get a list of all objectives listed in the file instead
    """
    if objectives is None:
        create = True
        objectives = list()
        print('Getting objectives from %s ...' % filename)
    else:
        create = False
        print('Updating objectives from %s ...' % filename)

    with open(filename, 'r') as file:
        lines = file.readlines()

        for line in lines[1:]:
            # first col is name
            vals = line.split(',')
            if create:
                objective = ObjectiveVar(vals[0], costFunctionType=CostFunctionType.Ignore)
                objectives.append(objective)
            else:
                objective = next((o for o in objectives if o.name == vals[0]), None)

            if objective is not None:
                objective.targetValue = float(vals[1])
                objective.std = float(vals[2])
    
    return objectives

def getRunNumber() -> str:
    """ Gets GenOpt run number using the name of the current working directory
    """
    cur_dirname = os.path.basename(os.getcwd())
    run_match = re.match(r'[\w-]*-(\d+)$', cur_dirname)

    if run_match is not None:
        return int(run_match[1])
    else:
        return 0

def getSafeLogDir(unsafeDir, safe_rollback = '..\\'):
    """ Try provided unsafeDir and falls back to parent dir otherwise
    """

    if not os.path.isdir(unsafeDir):
        return safe_rollback
    else:
        return unsafeDir + '\\'

def unifyCostFunc(o:ObjectiveVar):
    """ Changes the objective cost function type to variance
    """
    if o.costFunctionType == CostFunctionType.Linear:
        o.costFunctionType = CostFunctionType.LinearVariance
    elif o.costFunctionType == CostFunctionType.Quadratic:
        o.costFunctionType = CostFunctionType.QuadraticVariance

def countTotalWeightedCost(objectives : Iterable[ObjectiveVar]) -> float:
    """ Returns total objective costs, weighted by number of objectives - effectively returns mean of costs"""
    active_obj = sum(1 for o in objectives if o.costFunctionType is not CostFunctionType.Ignore)
    
    total_cost = sum(o.cost() for o in objectives)
    # weighing cost by numbr of objectives
    return total_cost / active_obj

def countTotalSumCost(objectives : Iterable[ObjectiveVar]) -> float:
    "Returns simple sum of all objectives costs"
    return sum(o.cost() for o in objectives)    

def getAxes(vars_set : dict) -> plt.axes:
    """ Gets axes from vars_set if defined, creates empty subplots figure otherwise 
    """

    if '__plot_axes' in vars_set:
        return vars_set['__plot_axes']
    else:
        fig = plt.figure()
        return fig.subplots()

def plotObjectiveTarget(pack:tuple, objective_name:str, unitFactor:float, fmt = 'k', verticalalignment = 'bottom', showVal = True):
    """ Plots the objective target with label
    pack = (objectives:list, ax:plt.axes, interval:range)
    """
    (objectives, time, ax, interval) = pack
    # get the bounds from the target value
    objective = next((o for o in objectives if o.name == objective_name))
    targetVal = objective.targetValue*unitFactor
    val = objective.value*unitFactor
    ax.plot((time[interval[0]], time[interval[-1]]), [targetVal]*2, fmt)
    if showVal:
        s = '%s = %2f (%.6f)' % (objective_name, val, objective.cost())
    else:
        s = '%s %.6f' % (objective_name, objective.cost())
    ax.text(time[interval[-1]], targetVal, s, 
            horizontalalignment='right', 
            verticalalignment=verticalalignment)

def plotObjectiveLimit(pack:tuple, objective_name:str, unitFactor:float, limit:str, fmt = 'k', verticalalignment = 'bottom', showVal = True):
    """ Plots the objective limit with label
    pack = (objectives:list, time:iterable, ax:plt.axes, interval:range)
    limit = 'lower' or 'upper'
    """
    (objectives, time, ax, interval) = pack
    SV_min_objective = getObjectiveByName(objectives, objective_name)
    limit_val = SV_min_objective.limit[0] if limit == 'lower' else SV_min_objective.limit[1]
    val = SV_min_objective.value*unitFactor
    ax.plot([time[interval[0]], time[interval[-1]]], [limit_val]*2, 'r')
    if showVal:
        s= '%s = %3f, %s lim at %3f (%.4f)' % (objective_name, val, limit, limit_val, SV_min_objective.cost())
    else:
        s = '%s %s lim %.4f' % (objective_name, limit, SV_min_objective.cost())
    ax.text(time[interval[-1]], limit_val, s, horizontalalignment='right', 
            verticalalignment=verticalalignment, fontsize = 8, color='red')

def importCostFunction(dir = '..\\'):
    """ imports cost_function.py from parent directory
    """

    spec = importlib.util.spec_from_file_location(
        'cost_function', dir + 'cost_function.py')
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)
    return cf

def checkSimulationLength(simulationTime, minimalSimulationTime):

    if simulationTime < minimalSimulationTime:
        try:
            # this must be errorproof
            frame = inspect.stack()[1]
            filename = frame[0].f_code.co_filename
        except:
            filename = 'Unknwon'

        raise SimulationResultIncompleteError(simulationTime, minimalSimulationTime, filename)
        
def writeToFile(filename, time:Iterable, signal:Iterable):
    with open(filename, 'w') as file:
        file.write('Time, var\n')
        for t, s in zip(time, signal):
            file.write('%.2f, %.6e\n' % (t, s))
    
    print("Written to %s, mate" % filename)