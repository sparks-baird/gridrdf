
import json
import math
import numpy as np
import pandas as pd
from collections import Counter

try:
    from ElMD import ElMD
    from pyemd import emd_with_flow
except:
    print('EMD module not installed')


def remove_nan():
    '''
    Remove NAN data from v6 by create a new list
    '''

    with open('MP_modulus_v8.json', 'r') as f:
        data = json.load(f)

    new_data = []
    for d in data:
        if not math.isnan(d['ave_bond_std']):
            new_data.append(d)

    with open('MP_modulus_v9.json', 'w') as f:
        json.dump(new_data, f, indent=1)


def emd_example():
    '''
    Test the Earth Mover's Distance (EMD) using similarity matrix 
    against the EMD in the literature
    https://github.com/lrcfmd/ElMD/
    '''
    elem_emd = ElMD()
    comp1 = elem_emd._gen_vector('Li0.7Al0.3Ti1.7P3O12')
    comp2 = elem_emd._gen_vector('La0.57Li0.29TiO3')

    petiffor_emd = elem_emd._EMD(comp1, comp2)

    comp1_reindex = pd.DataFrame(comp1, index=modified_petiffor)
    comp1_reindex = comp1_reindex.reindex(index=petiffor)
    comp2_reindex = pd.DataFrame(comp2, index=modified_petiffor)
    comp2_reindex = comp2_reindex.reindex(index=petiffor)

    dist_matrix = pd.read_csv('similarity_matrix.csv', index_col='ionA').values
    dist_matrix = dist_matrix.copy(order='C')

    em = emd_with_flow(comp1_reindex.values[:,0], comp2_reindex.values[:,0], dist_matrix)
    simi_matrix_emd = em[0]
    emd_flow = pd.DataFrame(em[1], columns=petiffor, index=petiffor)
    emd_flow.replace(0, np.nan).to_csv('emd_flow.csv')


def insert_field(infile1='num_shell', infile2='MP_modulus_all.json', 
                outfile='MP_modulus_v4.json'):
    '''
    Insert new file in the json file

    Args:
        infile1: file containing values to be inserted, the field should
            consult data_explore.py
        infile2: file into which new field will be inserted
        outfile: a new file contains new inserted fields
    Return:
        None
    '''
    results = np.loadtxt(infile1, delimiter=' ')
    with open(infile2, 'r') as f:
        data = json.load(f)

    for i, d in enumerate(data):
        d['average_bond_length'] = results[i][2]
        d['bond_length_std'] = results[i][3]    

    with open(outfile, 'w') as f:
        json.dump(data, f, indent=1)
    
    return


def analysis_emd_100():
    '''
    '''
    df = pd.DataFrame([], index=np.linspace(0.2, 0.6, 5))
    for i in ['small', 'middle', 'large']:
        for thresh in np.linspace(0.2, 0.6, 5):
            data = np.loadtxt(i + '_sample_' + str(thresh), delimiter=' ')
            for val in range(4):
                df.loc[thresh, i + str(val)] = np.count_nonzero(data == val)
    df.to_csv('analysis.csv')


if __name__ == '__main__':
    modified_petiffor = ['He', 'Ne', 'Ar', 'Kr', 'Xe', 'Rn', 
                    'Fr', 'Cs', 'Rb', 'K', 'Na', 'Li', 'Ra', 'Ba', 'Sr', 'Ca', 
                    'Eu', 'Yb', 'Lu', 'Tm', 'Y', 'Er', 'Ho', 'Dy', 'Tb', 'Gd', 'Sm', 'Pm', 'Nd', 'Pr', 'Ce', 'La', 
                    'Ac', 'Th', 'Pa', 'U', 'Np', 'Pu', 'Am', 'Cm', 'Bk', 'Cf', 'Es', 'Fm', 'Md', 'No', 'Lr', 
                    'Sc', 'Zr', 'Hf', 'Ti', 'Ta', 'Nb', 'V', 'Cr', 'Mo', 'W', 'Re', 
                    'Tc', 'Os', 'Ru', 'Ir', 'Rh', 'Pt', 'Pd', 'Au', 'Ag', 'Cu', 
                    'Ni', 'Co', 'Fe', 'Mn', 'Mg', 'Zn', 'Cd', 'Hg', 
                    'Be', 'Al', 'Ga', 'In', 'Tl', 'Pb', 'Sn', 'Ge', 'Si', 'B', 'C', 
                    'N', 'P', 'As', 'Sb', 'Bi', 'Po', 'Te', 'Se', 'S', 'O', 'At', 'I', 'Br', 'Cl', 'F', 'H']
    petiffor = ['Cs', 'Rb', 'K', 'Na', 'Li', 'Ba', 'Sr', 'Ca', 'Yb', 'Eu', 'Y',  'Sc', 'Lu', 'Tm', 'Er', 'Ho', 
                'Dy', 'Tb', 'Gd', 'Sm', 'Pm', 'Nd', 'Pr', 'Ce', 'La', 'Zr', 'Hf', 'Ti', 'Nb', 'Ta', 'V',  'Mo', 
                'W',  'Cr', 'Tc', 'Re', 'Mn', 'Fe', 'Os', 'Ru', 'Co', 'Ir', 'Rh', 'Ni', 'Pt', 'Pd', 'Au', 'Ag', 
                'Cu', 'Mg', 'Hg', 'Cd', 'Zn', 'Be', 'Tl', 'In', 'Al', 'Ga', 'Pb', 'Sn', 'Ge', 'Si', 'B',  'Bi', 
                'Sb', 'As', 'P',  'Te', 'Se', 'S', 'C', 'I', 'Br', 'Cl', 'N', 'O', 'F', 'H']
