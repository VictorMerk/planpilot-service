%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
% Encodes that if an action occurs at a time T, then it occurs at some time --- but abstract
% when specifically.
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

occurs_sometime(Action) :-  occurs(Action, T), T > 0.


#show occurs/2.
#show occurs_sometime/1.

% Also expose the state atoms so they surface as facets.
#show holds/3.
